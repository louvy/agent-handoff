"""Public CLI/MCP lifecycle checks with isolated native stores and worktrees."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import unittest
import uuid
import m1_cli
import m2_mcp


class StateTests(unittest.TestCase):
    def setUp(self):
        self.m = m2_mcp.McpTests()
        self.m.setUp()
        self.addCleanup(self.m.tearDown)
        self.f = self.m.fixture

    def snapshot(self):
        state = self.f.root / 'state'
        return {str(p.relative_to(state)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in state.rglob('*') if p.is_file()}

    def test_read_only_and_missing_state(self):
        f = self.f
        plan = f.preview()
        unknown = f.root / 'state' / str(uuid.uuid4())
        unknown.mkdir()
        with sqlite3.connect(f.root / 'state/handoff.sqlite3') as db:
            identity = db.execute("SELECT value FROM metadata WHERE key='state_id'").fetchone()[0]
        (unknown / '.handoff-building').write_text(identity)
        before = self.snapshot()
        for tool, args in [('get_handoff', {'handoff_id':plan['id']}),
                           ('read_context', {'handoff_id':plan['id'],'section':'transcript'}),
                           ('find_sessions', {'agent':'codex'})]:
            self.m.call(tool,args)
        self.assertEqual(before,self.snapshot())
        fresh = f.root / 'fresh'
        result = subprocess.run([m1_cli.BIN, '--state-dir',fresh,'--codex-root',f.source_root,
                                 'sessions'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(fresh.exists())
        database=f.root/'state/handoff.sqlite3'
        database.rename(database.with_suffix('.saved'))
        self.assertEqual(self.m.call('get_handoff',{'handoff_id':plan['id']},ok=False)['code'],'STATE_MISSING')
        self.assertTrue(unknown.exists())

    def test_deleted_notes_unbind_and_skipped_dependencies(self):
        f=self.f
        captured=f.cli('capture','source-id')
        f.note.unlink()
        self.assertFalse(f.cli('memory','list','--project',f.work)['bindings'][0]['available'])
        self.assertEqual(f.cli('memory','unbind','--project',f.work,'--path',f.note)['removed'],1)
        self.assertIn('only-in-memory-38a1',f.cli('read',captured['id'],'--section','memory')['text'])
        notes=f.root/'notes';notes.mkdir()
        (notes/'guide.md').write_text('guide-marker')
        for name in ['.git','node_modules','target']:
            (notes/name).mkdir()
            (notes/name/'large.md').write_text('x'*40000)
        f.cli('memory','bind','--project',f.work,'--path',notes)
        current=f.cli('capture','source-id')
        text=f.cli('read',current['id'],'--section','memory')['text']
        self.assertIn('guide-marker',text)
        self.assertNotIn('large.md',text)

    def test_backup_restore_old_new_and_protocol_progress(self):
        f=self.f
        reg=f.cli('register-agent','--id','example','--role','both','--protocol-version','1')
        v=f.cli('capture','source-id')
        old=f.root/'old'
        f.cli('backup',old)
        a=f.cli('assign',v['id'],'--to','example','--instructions','after-backup')
        plan=f.cli('claim',a['id'],'--recipient','example')['plan']
        target=Path(plan['target_path'])
        wire=json.loads(target.read_text())
        wire['messages'].append({'role':'assistant','timestamp':'2026-01-01T00:00:00Z',
                                 'content':[{'type':'text','text':'continued after backup'}]})
        target.write_text(json.dumps(wire))
        latest=f.root/'latest';f.cli('backup',latest)
        wire['messages'].append({'role':'assistant','timestamp':'2026-01-01T00:00:01Z',
                                 'content':[{'type':'text','text':'newest progress'}]})
        target.write_text(json.dumps(wire))
        restored=f.cli('restore',old)
        self.assertTrue(Path(restored['rollback_backup']).is_dir())
        for _ in range(2): f.cli('versions')
        self.assertTrue((f.root/'state'/plan['id']/'native.jsonl').is_file())
        f.cli('restore',latest)
        again=f.cli('claim',a['id'],'--recipient','example')['plan']
        self.assertEqual(again['target_id'],plan['target_id'])
        self.assertIn('newest progress',target.read_text())
        self.assertEqual(f.cli('register-agent','--id','example','--role','both','--protocol-version','1'),reg)
        damaged=f.root/'damaged';shutil.copytree(latest,damaged)
        (damaged/'handoff.sqlite3').write_bytes(b'bad')
        self.assertEqual(json.loads(f.cli('restore',damaged,ok=False))['code'],'BACKUP_INVALID')
        self.assertEqual(f.cli('claim',a['id'],'--recipient','example')['plan']['target_id'],plan['target_id'])

    def test_dedup_migration_paging_and_head_changes(self):
        f=self.f
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            versions=list(pool.map(lambda _:f.cli('capture','source-id'),range(4)))
        self.assertEqual(len({v['id'] for v in versions}),1)
        for i in range(4):
            with f.source.open('a') as out: out.write('\n'*(i+1))
            f.cli('capture','source-id')
        first=f.cli('versions','--limit','2')
        # A new item inserted between pages must not duplicate the prior page.
        f.source.write_text(f.source.read_text()+'\n')
        f.cli('capture','source-id')
        second=self.m.call('list_versions',{'limit':2,'cursor':first['next_cursor']})
        self.assertEqual(len(first['versions']),2)
        self.assertFalse({v['id'] for v in first['versions']} & {v['id'] for v in second['versions']})
        with sqlite3.connect(f.root/'state/handoff.sqlite3') as db:
            query=db.execute("EXPLAIN QUERY PLAN SELECT body FROM versions WHERE source_agent=? AND source_id=? AND source_path=? ORDER BY rowid DESC LIMIT 1",
                             ('codex','source-id',str(f.source))).fetchall()
            self.assertIn('version_source',str(query))
        head=f.cli('capture','source-id')
        f.git(f.work,'-c','user.name=Test','-c','user.email=test@example.invalid',
              '-c','commit.gpgsign=false','commit','--allow-empty','-qm','new head')
        self.assertNotEqual(head['id'],f.cli('capture','source-id')['id'])
        for _ in range(3): f.cli('assign',head['id'],'--to','codex','--instructions','next')
        self.assertEqual(len(f.cli('inbox','--recipient','codex','--limit','2')['assignments']),2)

    def test_gc_archive_references_and_recovery(self):
        f=self.f
        v=f.cli('capture','source-id')
        a=f.cli('assign',v['id'],'--to','codex','--instructions','resume')
        self.assertEqual(json.loads(f.cli('archive',v['task_id'],ok=False))['code'],'TASK_PENDING')
        p=f.cli('claim',a['id'],'--recipient','codex')['plan']
        target=Path(p['target_path'])
        before=f.root/'before-gc';f.cli('backup',before)
        f.cli('archive',v['task_id'])
        unknown=f.root/'state'/str(uuid.uuid4());unknown.mkdir()
        (unknown/'.handoff-commit-intent').write_text('unknown')
        preview=f.cli('gc','--older-than','0d')
        self.assertIn(p['id'],preview['plans'])
        self.assertTrue((f.root/'state'/p['id']).exists())
        result=f.cli('gc','--older-than','0d','--execute')
        self.assertIn(v['id'],result['versions'])
        self.assertTrue(target.exists())
        self.assertTrue(unknown.exists())
        self.assertFalse((f.root/'state'/p['id']).exists())
        self.assertEqual(f.cli('gc','--older-than','0d','--execute')['plans'],[])
        f.cli('restore',before)
        self.assertEqual(f.cli('claim',a['id'],'--recipient','codex')['plan']['target_id'],p['target_id'])
        # Simulate process death before the GC transaction commits.
        run=str(uuid.uuid4());q=f.root/'state'/('.handoff-gc-'+run);q.mkdir()
        with sqlite3.connect(f.root/'state/handoff.sqlite3') as db:
            identity=db.execute("SELECT value FROM metadata WHERE key='state_id'").fetchone()[0]
        (q/'journal.json').write_text(json.dumps({'state_id':identity,'plans':[p['id']]}))
        (f.root/'state'/p['id']).rename(q/p['id'])
        f.cli('capture','source-id')
        self.assertTrue((f.root/'state'/p['id']).is_dir())
        self.assertFalse(q.exists())


    def test_old_schema_read_and_migration(self):
        f=self.f
        v=f.cli('capture','source-id')
        database=f.root/'state/handoff.sqlite3'
        with sqlite3.connect(database) as db:
            for table,base in [('versions','id TEXT PRIMARY KEY, body TEXT NOT NULL'),
                               ('plans','id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL'),
                               ('assignments','id TEXT PRIMARY KEY, body TEXT NOT NULL, plan_id TEXT')]:
                columns='id,body,status' if table=='plans' else 'id,body,plan_id' if table=='assignments' else 'id,body'
                rows=db.execute(f'SELECT {columns} FROM {table}').fetchall()
                db.execute(f'DROP TABLE {table}')
                db.execute(f'CREATE TABLE {table}({base})')
                db.executemany(f'INSERT INTO {table} VALUES({",".join("?" for _ in columns.split(","))})',rows)
        self.assertEqual(f.cli('versions','--task-id',v['task_id'])['versions'][0]['id'],v['id'])
        self.assertEqual(f.cli('capture','source-id')['id'],v['id'])
        with sqlite3.connect(database) as db:
            self.assertIn('source_id',[r[1] for r in db.execute('PRAGMA table_xinfo(versions)')])

    def test_gc_keeps_ancestor_for_active_fork(self):
        f=self.f
        v=f.cli('capture','source-id')
        plan=f.cli('prepare',v['id'],'--to','codex')
        f.cli('apply',plan['id'])
        child=f.cli('capture',plan['target_id'])
        database=f.root/'state/handoff.sqlite3'
        with sqlite3.connect(database) as db:
            body=json.loads(db.execute('SELECT body FROM versions WHERE id=?',(child['id'],)).fetchone()[0])
            body['task_id']='independent-active-fork'
            db.execute('UPDATE versions SET body=? WHERE id=?',(json.dumps(body),child['id']))
        f.cli('archive',v['task_id'])
        report=f.cli('gc','--older-than','0d','--execute')
        self.assertNotIn(v['id'],report['versions'])
        self.assertNotIn(plan['id'],report['plans'])
        self.assertEqual(f.cli('status',plan['id'])['id'],plan['id'])
