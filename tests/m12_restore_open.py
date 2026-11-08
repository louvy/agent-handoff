"""Launcher completion must update the database active after concurrent restore."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time
import unittest
import m1_cli


class OpenRestoreTests(unittest.TestCase):
    def setUp(self):
        self.f=m1_cli.CliTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def start(self,*command):
        f=self.f
        marker=f.root/'launcher-entered'
        release=f.root/'launcher-release'
        launcher=f.root/'launcher'
        launcher.write_text("#!/usr/bin/env python3\nimport pathlib,time\n"+
            f"pathlib.Path({str(marker)!r}).touch()\n"+
            f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(.01)\n")
        launcher.chmod(0o700)
        proc=subprocess.Popen([m1_cli.BIN,'--state-dir',f.root/'state',
            '--codex-root',f.source_root,'--claude-root',f.root/'claude',*command],
            env={**os.environ,'HOME':str(f.root),'HANDOFF_CLAUDE_BIN':str(launcher)},
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
        def cleanup():
            if proc.poll() is None: os.killpg(proc.pid,signal.SIGKILL)
            proc.communicate(timeout=3)
        self.addCleanup(cleanup)
        deadline=time.monotonic()+5
        while not marker.exists() and time.monotonic()<deadline and proc.poll() is None:
            time.sleep(.01)
        self.assertTrue(marker.exists(),'launcher never started')
        return proc,release

    def same_plan(self,claim):
        f=self.f
        v=f.cli('capture','source-id')
        if claim:
            a=f.cli('assign',v['id'],'--to','claude-desktop','--instructions','continue original assignment')
            plan=f.cli('claim',a['id'],'--recipient','claude-desktop')['plan']
            command=['claim',a['id'],'--recipient','claude-desktop','--open']
        else:
            plan=f.cli('prepare',v['id'],'--to','claude-desktop')
            f.cli('apply',plan['id'])
            command=['apply',plan['id'],'--open']
        saved=f.root/'saved'
        f.cli('backup',saved)
        proc,release=self.start(*command)
        restored=f.cli('restore',saved)
        self.assertEqual(f.cli('status',plan['id'])['status'],'waiting_for_open')
        release.touch()
        stdout,stderr=proc.communicate(timeout=5)
        self.assertEqual(proc.returncode,0,stderr)
        reply=json.loads(stdout)
        self.assertEqual(reply['status'],'open_requested')
        self.assertEqual(f.cli('status',plan['id'])['status'],'open_requested')
        if claim:self.assertEqual(reply['plan']['status'],'open_requested')
        with sqlite3.connect(Path(restored['rollback_state'])/'handoff.sqlite3') as db:
            self.assertEqual(db.execute('SELECT status FROM plans WHERE id=?',(plan['id'],)).fetchone()[0],'waiting_for_open')

    def test_apply_completion_uses_active_database(self):
        self.same_plan(False)

    def test_claim_completion_uses_active_database(self):
        self.same_plan(True)

    def test_missing_plan_after_restore_does_not_report_open_requested(self):
        f=self.f
        v=f.cli('capture','source-id')
        older=f.root/'older';f.cli('backup',older)
        p=f.cli('prepare',v['id'],'--to','claude-desktop');f.cli('apply',p['id'])
        proc,release=self.start('apply',p['id'],'--open')
        restored=f.cli('restore',older)
        release.touch()
        stdout,stderr=proc.communicate(timeout=5)
        self.assertNotEqual(proc.returncode,0,stdout)
        self.assertEqual(json.loads(stderr)['code'],'PLAN_NOT_FOUND')
        self.assertTrue((f.root/'state'/p['id']).is_dir())
        with sqlite3.connect(Path(restored['rollback_state'])/'handoff.sqlite3') as db:
            self.assertEqual(db.execute('SELECT status FROM plans WHERE id=?',(p['id'],)).fetchone()[0],'waiting_for_open')
