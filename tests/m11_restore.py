"""Restore completeness, failure isolation, bounded writers and side-effect-free queries."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
import m1_cli
import m2_mcp


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.m=m2_mcp.McpTests()
        self.m.setUp()
        self.addCleanup(self.m.tearDown)
        self.f=self.m.fixture

    def tree(self):
        state=self.f.root/'state'
        return {str(p.relative_to(state)):(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mode)
                for p in state.rglob('*') if p.is_file()}

    def protocol(self):
        f=self.f
        f.cli('register-agent','--id','external','--role','both','--protocol-version','1')
        v=f.cli('capture','source-id')
        a=f.cli('assign',v['id'],'--to','external','--instructions','restore me')
        plan=f.cli('claim',a['id'],'--recipient','external')['plan']
        return a,plan

    def test_protocol_newer_backup_corrupt_current_and_divergence(self):
        f=self.f
        a,p=self.protocol()
        target=Path(p['target_path'])
        original=target.read_bytes()
        wire=json.loads(original)
        wire['messages'].append({'role':'assistant','timestamp':'2026-01-01T00:00:00Z',
                                 'content':[{'type':'text','text':'saved progress'}]})
        latest=json.dumps(wire).encode()
        target.write_bytes(latest)
        backup=f.root/'saved';f.cli('backup',backup)
        for current in [original,b'',b'{broken']:
            with self.subTest(current=current[:10]):
                target.write_bytes(current)
                f.cli('restore',backup)
                self.assertIn('saved progress',target.read_text())
                self.assertEqual(f.cli('claim',a['id'],'--recipient','external')['plan']['target_id'],p['target_id'])
        wire['messages'][-1]['content'][0]['text']='divergent local progress'
        target.write_text(json.dumps(wire))
        before=self.tree()
        self.assertEqual(json.loads(f.cli('restore',backup,ok=False))['code'],'RESTORE_CONFLICT')
        self.assertEqual(self.tree(),before)

    def test_empty_registered_agent_and_missing_state(self):
        f=self.f
        reg=f.cli('register-agent','--id','empty-agent','--role','both','--protocol-version','1')
        version=f.cli('capture','source-id')
        a=f.cli('assign',version['id'],'--to','empty-agent','--instructions','continue')
        backup=f.root/'backup';f.cli('backup',backup)
        (f.root/'state').rename(f.root/'original')
        f.cli('restore',backup)
        self.assertTrue(Path(reg['sessions_dir']).is_dir())
        self.assertEqual(f.cli('claim',a['id'],'--recipient','empty-agent')['status'],'waiting_for_open')

    def test_conflict_has_no_partial_writes(self):
        f=self.f
        p=f.preview();f.cli('apply',p['id'])
        saved=f.root/'saved';f.cli('backup',saved)
        folder=f.root/'state'/p['id']
        (folder/'.handoff-commit-intent').unlink()
        (folder/'transcript.txt').write_text('conflicting local file')
        before=self.tree()
        self.assertEqual(json.loads(f.cli('restore',saved,ok=False))['code'],'RESTORE_CONFLICT')
        self.assertEqual(before,self.tree())
        self.assertFalse((folder/'.handoff-commit-intent').exists())

    def test_corrupt_database_restored_and_original_kept(self):
        f=self.f
        p=f.preview()
        database=f.root/'state/handoff.sqlite3'
        # Legacy plans default to Claude when target_agent was not persisted.
        with sqlite3.connect(database) as db:
            body=json.loads(db.execute('SELECT body FROM plans WHERE id=?',(p['id'],)).fetchone()[0])
            body.pop('target_agent')
            db.execute('UPDATE plans SET body=? WHERE id=?',(json.dumps(body),p['id']))
        saved=f.root/'saved';f.cli('backup',saved)
        for damaged in [b'corrupt database',b'']:
            with self.subTest(damaged=damaged):
                database.write_bytes(damaged)
                restored=f.cli('restore',saved)
                self.assertEqual((Path(restored['rollback_state'])/'handoff.sqlite3').read_bytes(),damaged)
                self.assertEqual(f.cli('apply',p['id'])['id'],p['id'])

    def hold_lock(self):
        lock=self.f.root/'.state.handoff-lock'
        proc=subprocess.Popen([sys.executable,'-u','-c',
            'import fcntl,sys; f=open(sys.argv[1],"a"); fcntl.flock(f,fcntl.LOCK_EX); print("ready"); sys.stdin.read()',
            str(lock)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.assertEqual(proc.stdout.readline().strip(),'ready')
        def cleanup():
            proc.communicate(timeout=3)
        self.addCleanup(cleanup)
        return proc

    def test_writer_deadline_and_mcp_disconnect(self):
        f=self.f
        v=f.cli('capture','source-id')
        held=self.hold_lock()
        start=time.monotonic()
        error=json.loads(f.cli('--lock-timeout-seconds','1','capture','source-id',ok=False))
        self.assertEqual(error['code'],'STATE_BUSY')
        self.assertTrue(error['retryable'])
        self.assertLess(time.monotonic()-start,2.5)
        self.assertEqual(f.cli('versions')['versions'][0]['id'],v['id'])
        self.m.send('tools/call',{'name':'capture_version','arguments':{'source_agent':'codex','source_ref':'source-id'}})
        time.sleep(.1)
        self.m.process.stdin.close()
        self.m.process.wait(timeout=2)
        held.communicate(timeout=3)
        self.assertEqual(f.cli('capture','source-id')['id'],v['id'])

    def test_readonly_no_sidecars_and_readonly_directory(self):
        f=self.f
        p=f.preview()
        state=f.root/'state'
        self.assertFalse((state/'handoff.sqlite3-wal').exists())
        before=self.tree()
        self.m.call('get_handoff',{'handoff_id':p['id']})
        self.m.call('read_context',{'handoff_id':p['id'],'section':'transcript'})
        self.assertEqual(before,self.tree())
        database=state/'handoff.sqlite3'
        state.chmod(0o500);database.chmod(0o400)
        try:
            self.assertEqual(f.cli('status',p['id'])['id'],p['id'])
            self.assertFalse((state/'handoff.sqlite3-wal').exists())
            self.assertFalse((state/'handoff.sqlite3-shm').exists())
        finally:
            database.chmod(0o600);state.chmod(0o700)

    def test_readonly_includes_live_committed_wal(self):
        f=self.f
        v=f.cli('capture','source-id')
        database=f.root/'state/handoff.sqlite3'
        with sqlite3.connect(database) as db:
            body=json.loads(db.execute('SELECT body FROM versions WHERE id=?',(v['id'],)).fetchone()[0])
            body['id']='wal-only-version'
            db.execute('INSERT INTO versions(id,body) VALUES(?,?)',(body['id'],json.dumps(body)))
            db.commit()
            self.assertTrue(database.with_name('handoff.sqlite3-wal').exists())
            before=self.tree()
            versions=self.m.call('list_versions',{})['versions']
            self.assertIn('wal-only-version',[v['id'] for v in versions])
            self.assertEqual(before,self.tree())

    def test_legacy_lock_also_has_deadline(self):
        f=self.f
        legacy=f.root/'state/.handoff-operation.lock'
        held=subprocess.Popen([sys.executable,'-u','-c',
            'import fcntl,sys; f=open(sys.argv[1],"a"); fcntl.flock(f,fcntl.LOCK_EX); print("ready"); sys.stdin.read()',
            str(legacy)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.assertEqual(held.stdout.readline().strip(),'ready')
        try:
            start=time.monotonic()
            error=json.loads(f.cli('--lock-timeout-seconds','1','capture','source-id',ok=False))
            self.assertEqual(error['code'],'STATE_BUSY')
            self.assertLess(time.monotonic()-start,2.5)
        finally:
            held.communicate(timeout=3)
        self.assertTrue(f.cli('capture','source-id')['id'])

    def test_recover_interrupted_directory_activation(self):
        f=self.f
        p=f.preview()
        import shutil
        state=f.root/'state'
        staged=f.root/'.handoff-restore-interrupted'
        shutil.copytree(state,staged)
        rollback=f.root/'handoff-rollback-interrupted'
        journal=f.root/'.state.handoff-restore.json'
        journal.write_text(json.dumps({'state':str(state),'staged':str(staged),'rollback':str(rollback)}))
        state.rename(rollback)
        # Queries preserve the incomplete operation, then a writer restores it.
        self.assertEqual(json.loads(f.cli('status',p['id'],ok=False))['code'],'STATE_MISSING')
        self.assertTrue(journal.exists())
        f.cli('capture','source-id')
        self.assertTrue(state.exists())
        self.assertFalse(staged.exists())
        self.assertFalse(journal.exists())
        self.assertEqual(f.cli('apply',p['id'])['id'],p['id'])

    def test_old_backup_gc_keeps_newer_plan_files(self):
        f=self.f
        v=f.cli('capture','source-id')
        old_plan=f.cli('prepare',v['id'],'--to','codex')
        f.cli('apply',old_plan['id'])
        f.cli('archive',v['task_id'])
        old=f.root/'old';f.cli('backup',old)
        a=f.cli('assign',v['id'],'--to','claude-desktop','--instructions','created later')
        plan=f.cli('claim',a['id'],'--recipient','claude-desktop')['plan']
        latest=f.root/'latest';f.cli('backup',latest)
        folder=f.root/'state'/plan['id']
        before={str(p.relative_to(folder)):p.read_bytes() for p in folder.rglob('*') if p.is_file()}
        f.cli('restore',old)
        gc=f.cli('gc','--older-than','0d','--execute')
        self.assertIn(old_plan['id'],gc['plans'])
        self.assertEqual(before,{str(p.relative_to(folder)):p.read_bytes() for p in folder.rglob('*') if p.is_file()})
        f.cli('restore',latest)
        self.assertEqual(f.cli('claim',a['id'],'--recipient','claude-desktop')['plan']['id'],plan['id'])

    def test_readonly_rollback_journal_keeps_uncommitted_rows_hidden(self):
        f=self.f
        v=f.cli('capture','source-id')
        database=f.root/'state/handoff.sqlite3'
        with sqlite3.connect(database) as db:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('PRAGMA cache_size=1')
            body=json.loads(db.execute('SELECT body FROM versions WHERE id=?',(v['id'],)).fetchone()[0])
            body['id']='uncommitted'
            body['messages'][0]['content'][0]['text']='x'*100000
            db.execute('INSERT INTO versions(id,body) VALUES(?,?)',(body['id'],json.dumps(body)))
            before=self.tree()
            versions=self.m.call('list_versions',{})['versions']
            self.assertNotIn('uncommitted',[v['id'] for v in versions])
            self.assertEqual(before,self.tree())
            db.rollback()

    def test_invalid_source_candidate_can_restore_but_truncated_target_cannot(self):
        f=self.f
        f.cli('register-agent','--id','raw-source','--role','source','--protocol-version','1')
        source=f.root/'state/agents/raw-source/bad.json'
        source.write_text('invalid source candidate')
        source_backup=f.root/'source-backup';f.cli('backup',source_backup)
        (f.root/'state').rename(f.root/'source-original')
        f.cli('restore',source_backup)
        self.assertEqual(source.read_text(),'invalid source candidate')
        self.assertTrue(f.cli('sessions','--agent','raw-source')['partial'])
        _,p=self.protocol()
        target=Path(p['target_path'])
        wire=json.loads(target.read_text());wire['messages']=wire['messages'][:1]
        target.write_text(json.dumps(wire))
        target_backup=f.root/'bad-target-backup';f.cli('backup',target_backup)
        before=self.tree()
        self.assertEqual(json.loads(f.cli('restore',target_backup,ok=False))['code'],'RESTORE_CONFLICT')
        self.assertEqual(before,self.tree())
