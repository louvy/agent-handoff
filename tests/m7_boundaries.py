"""验证原生目标身份、发现阶段警告、事务回滚与进程中断恢复。"""
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import time
import unittest
import uuid

import m1_cli
import m2_mcp


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.server = m2_mcp.McpTests()
        self.server.setUp()
        self.f = self.server.fixture

    def tearDown(self):
        self.server.tearDown()

    def claim_error(self, assignment, recipient, expected):
        cli = json.loads(self.f.cli('claim', assignment, '--recipient', recipient, ok=False))
        mcp = self.server.call('claim_assignment',
            {'assignment_id': assignment, 'recipient': recipient}, ok=False)
        self.assertEqual(cli, mcp)
        self.assertEqual(cli['code'], expected)
        self.assertTrue(cli['next_action'])
        self.assertIsInstance(cli['retryable'], bool)
        return cli

    def assignment(self, recipient):
        version = self.f.cli('capture', 'source-id')
        return self.f.cli('assign', version['id'], '--to', recipient, '--instructions', 'boundary receipt')

    def orphans(self):
        with sqlite3.connect(self.f.root / 'state/handoff.sqlite3') as db:
            ids = {r[0] for r in db.execute('SELECT id FROM plans')}
        return [p for p in (self.f.root / 'state').glob('*/transcript.txt')
                if p.parent.name not in ids]

    def test_missing_database_preserves_plan_and_does_not_create_empty_database(self):
        plan = self.f.cli('preview', 'source-id')
        state = self.f.root / 'state'
        database = state / 'handoff.sqlite3'
        held = state / 'held.sqlite3'
        folder = state / plan['id']
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                if legacy:
                    (state / '.handoff-state').unlink()
                original = {str(p.relative_to(folder)): p.read_bytes()
                    for p in folder.rglob('*') if p.is_file()}
                database.rename(held)
                try:
                    cli = json.loads(self.f.cli('versions', ok=False))
                    mcp = self.server.call('list_versions', {}, ok=False)
                    self.assertEqual(cli, mcp)
                    self.assertEqual(cli['code'], 'STATE_MISSING')
                    self.assertFalse(database.exists())
                    self.assertEqual(original, {str(p.relative_to(folder)): p.read_bytes()
                        for p in folder.rglob('*') if p.is_file()})
                finally:
                    held.rename(database)
                self.assertEqual(self.f.cli('apply', plan['id'])['target_id'], plan['target_id'])

    def test_foreign_and_unowned_uuid_folders_are_preserved(self):
        state = self.f.root / 'state'
        foreign = state / str(uuid.uuid4())
        foreign.mkdir()
        (foreign / 'valuable.txt').write_bytes(b'foreign data')
        # Old snapshots without an ownership marker cannot safely be auto-deleted.
        unowned = state / str(uuid.uuid4())
        unowned.mkdir()
        (unowned / 'transcript.txt').write_bytes(b'legacy history')
        wrong_owner = state / str(uuid.uuid4())
        wrong_owner.mkdir()
        (wrong_owner / '.handoff-building').write_text(str(uuid.uuid4()))
        (wrong_owner / 'transcript.txt').write_bytes(b'other database history')
        self.f.cli('versions')
        self.server.call('list_versions', {})
        self.assertEqual((foreign / 'valuable.txt').read_bytes(), b'foreign data')
        self.assertEqual((unowned / 'transcript.txt').read_bytes(), b'legacy history')
        self.assertEqual((wrong_owner / 'transcript.txt').read_bytes(), b'other database history')

    def test_temporary_target_permissions_preserve_classification_and_recover(self):
        for agent in ('codex', 'claude-desktop', 'deepseek'):
            with self.subTest(agent=agent):
                a = self.assignment(agent)
                claimed = self.f.cli('claim', a['id'], '--recipient', agent)
                target = Path(claimed['plan']['target_path'])
                original = target.read_bytes()
                mode = target.stat().st_mode & 0o777
                target.chmod(0)
                try:
                    issue = self.claim_error(a['id'], agent, 'ACCESS_DENIED')
                    self.assertTrue(issue['retryable'])
                    self.assertNotIn('备份', issue['next_action'])
                finally:
                    target.chmod(mode)
                self.assertEqual(target.read_bytes(), original)
                retried = self.f.cli('claim', a['id'], '--recipient', agent)
                self.assertEqual(retried['plan']['target_id'], claimed['plan']['target_id'])

    def test_temporary_sdk_failure_preserves_classification_and_recovers(self):
        missing_sdk = self.f.root / 'missing-sdk'
        broken_sdk = self.f.root / 'broken-sdk'
        broken_sdk.mkdir()
        for sdk in (missing_sdk, broken_sdk):
            with self.subTest(sdk=sdk.name):
                # CLI and the second MCP process use the same isolated database.
                other = m2_mcp.McpTests()
                other.extra_args = ['--dsh-package', str(sdk)]
                other.setUp()
                try:
                    other_a = other.fixture.cli('capture', 'source-id')
                    assignment = other.fixture.cli('assign', other_a['id'], '--to', 'deepseek',
                        '--instructions', 'SDK recovery')
                    imported = other.fixture.cli('claim', assignment['id'], '--recipient', 'deepseek')
                    other_target = Path(imported['plan']['target_path'])
                    other_original = other_target.read_bytes()
                    cli = json.loads(other.fixture.cli('--dsh-package', str(sdk),
                        'claim', assignment['id'], '--recipient', 'deepseek', ok=False))
                    mcp = other.call('claim_assignment',
                        {'assignment_id': assignment['id'], 'recipient': 'deepseek'}, ok=False)
                    self.assertEqual(cli, mcp)
                    self.assertEqual(cli['code'], 'ADAPTER_UNAVAILABLE')
                    self.assertTrue(cli['retryable'])
                    self.assertNotIn('备份', cli['next_action'])
                    retried = other.fixture.cli('claim', assignment['id'], '--recipient', 'deepseek')
                    self.assertEqual(retried['plan']['target_id'], imported['plan']['target_id'])
                    self.assertEqual(other_target.read_bytes(), other_original)
                finally:
                    other.tearDown()


    def test_older_same_database_backup_cannot_remove_committed_plans(self):
        state = self.f.root / 'state'
        database = state / 'handoff.sqlite3'
        old = self.f.root / 'old-backup.sqlite3'
        latest = self.f.root / 'latest-backup.sqlite3'

        def backup(path):
            with sqlite3.connect(database) as source, sqlite3.connect(path) as dest:
                source.backup(dest)

        backup(old)
        a = self.f.cli('preview', 'source-id')
        version = self.f.cli('capture', 'source-id')
        b = self.server.call('prepare_handoff', {'version_id': version['id'], 'target': 'codex'})
        # Exercise old product markers as well as a normal v2 completed preparation.
        identity = sqlite3.connect(database)
        try:
            owner = identity.execute("SELECT value FROM metadata WHERE key='state_id'").fetchone()[0]
        finally:
            identity.close()
        # Reproduce a plan written by the previous product version, which had no
        # independent commit receipt and left its legacy preparation marker behind.
        (state / b['id'] / '.handoff-commit-intent').unlink()
        (state / b['id'] / '.handoff-preparing').write_text(owner)
        backup(latest)
        originals = {}
        for plan in (a, b):
            folder = state / plan['id']
            originals[plan['id']] = {str(p.relative_to(folder)): p.read_bytes()
                for p in folder.rglob('*') if p.is_file()}
        # A transaction can commit before finish removes its building marker.
        (state / a['id'] / '.handoff-building').write_text(owner)
        originals[a['id']]['.handoff-building'] = owner.encode()
        try:
            shutil.copyfile(old, database)
            for _ in range(2):
                self.f.cli('versions')
                self.server.call('list_versions', {})
            for plan in (a, b):
                folder = state / plan['id']
                self.assertEqual(originals[plan['id']], {str(p.relative_to(folder)): p.read_bytes()
                    for p in folder.rglob('*') if p.is_file()})
        finally:
            shutil.copyfile(latest, database)
        for plan in (a, b):
            applied = self.f.cli('apply', plan['id'])
            self.assertEqual(applied['target_id'], plan['target_id'])

    def test_interrupted_commit_window_is_retained_without_database_row(self):
        a = self.assignment('claude-desktop')
        state = self.f.root / 'state'
        with sqlite3.connect(state / 'handoff.sqlite3') as db:
            db.execute("""CREATE TRIGGER pause_commit BEFORE INSERT ON plans BEGIN
                WITH RECURSIVE delay(x) AS (VALUES(1) UNION ALL
                SELECT x+1 FROM delay WHERE x<100000000) SELECT sum(x) FROM delay; END""")
        args = [m1_cli.BIN, '--state-dir', state, '--codex-root', self.f.source_root,
                '--claude-root', self.f.root / 'claude', 'claim', a['id'],
                '--recipient', 'claude-desktop']
        process = subprocess.Popen(args, env={**os.environ, 'HOME': str(self.f.root)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            deadline = time.monotonic() + 10
            markers = []
            while not markers and time.monotonic() < deadline and process.poll() is None:
                markers = list(state.glob('*/.handoff-commit-intent'))
                time.sleep(0.01)
            self.assertTrue(markers, 'commit window not reached')
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        folder = markers[0].parent
        original = (folder / 'transcript.txt').read_bytes()
        self.f.cli('versions')
        self.server.call('list_versions', {})
        self.assertEqual((folder / 'transcript.txt').read_bytes(), original)
        with sqlite3.connect(state / 'handoff.sqlite3') as db:
            db.execute('DROP TRIGGER pause_commit')
        retry = self.f.cli('claim', a['id'], '--recipient', 'claude-desktop')
        self.assertEqual(retry['status'], 'waiting_for_open')
        self.assertEqual((folder / 'transcript.txt').read_bytes(), original)

    def test_empty_and_replaced_targets_all_agents(self):
        for agent in ('codex', 'claude-desktop', 'deepseek'):
            with self.subTest(agent=agent):
                a = self.assignment(agent)
                claim = self.f.cli('claim', a['id'], '--recipient', agent)
                target = Path(claim['plan']['target_path'])
                original = target.read_bytes()
                other = self.f.cli('preview', 'source-id', '--to', agent)
                self.f.cli('apply', other['id'])
                replacement = Path(other['target_path']).read_bytes()
                for body in (b'', replacement):
                    target.write_bytes(body)
                    self.claim_error(a['id'], agent, 'TARGET_INVALID')
                    self.assertEqual(target.read_bytes(), body)
                target.write_bytes(original)
                restored = self.f.cli('claim', a['id'], '--recipient', agent)
                self.assertEqual(restored['plan']['target_id'], claim['plan']['target_id'])
                source_agent = 'claude' if agent == 'claude-desktop' else agent
                reread = self.f.cli('capture', claim['plan']['target_id'], '--from', source_agent)
                self.assertEqual(reread['source_id'], claim['plan']['target_id'])

    def test_invalid_section_has_same_cli_mcp_contract(self):
        version = self.f.cli('capture', 'source-id')
        cli = json.loads(self.f.cli('read', version['id'], '--section', 'invalid', ok=False))
        mcp = self.server.call('read_context',
            {'handoff_id': version['id'], 'section': 'invalid'}, ok=False)
        self.assertEqual(cli, mcp)
        self.assertEqual(cli['code'], 'INVALID_REQUEST')
        for section in ('memory', 'transcript'):
            self.assertIn(section, cli['next_action'])
            self.assertTrue(self.server.call('read_context',
                {'handoff_id': version['id'], 'section': section})['text'])

    def test_bad_header_reports_partial_and_exact_path_invalid(self):
        path = self.f.source.with_name('rollout-broken-header.jsonl')
        lines = self.f.source.read_text().splitlines()
        path.write_text('{invalid header\n' + '\n'.join(lines[1:]) + '\n')
        listing = self.server.call('find_sessions', {'agent': 'codex', 'query': 'source-id'})
        self.assertTrue(any(i['id'] == 'source-id' for i in listing['sessions']))
        self.assertTrue(listing['partial'])
        self.assertTrue(any(w['source_ref'] == str(path) for w in listing['warnings']))
        cli = json.loads(self.f.cli('capture', str(path), ok=False))
        mcp = self.server.call('capture_version',
            {'source_ref': str(path), 'source_agent': 'codex'}, ok=False)
        self.assertEqual(cli, mcp)
        self.assertEqual(cli['code'], 'SOURCE_INVALID')
        self.assertTrue(self.f.cli('capture', 'source-id')['id'])

    def test_assignment_rollback_removes_prepared_history(self):
        a = self.assignment('claude-desktop')
        database = self.f.root / 'state/handoff.sqlite3'
        with sqlite3.connect(database) as db:
            db.execute("""CREATE TRIGGER stop_binding BEFORE UPDATE OF plan_id ON assignments
                BEGIN SELECT RAISE(ABORT, 'test binding rollback'); END""")
        cli_error = json.loads(self.f.cli('claim', a['id'], '--recipient', 'claude-desktop', ok=False))
        # Inspect SQLite/files directly before another API call could perform recovery.
        self.assertEqual(self.orphans(), [])
        mcp_error = self.server.call('claim_assignment',
            {'assignment_id': a['id'], 'recipient': 'claude-desktop'}, ok=False)
        self.assertEqual(cli_error, mcp_error)
        self.assertEqual(cli_error['code'], 'STATE_UNAVAILABLE')
        with sqlite3.connect(database) as db:
            self.assertIsNone(db.execute('SELECT plan_id FROM assignments WHERE id=?', [a['id']]).fetchone()[0])
            db.execute('DROP TRIGGER stop_binding')
        claimed = self.f.cli('claim', a['id'], '--recipient', 'claude-desktop')
        self.assertEqual(claimed['status'], 'waiting_for_open')
        self.assertEqual(self.orphans(), [])

    def test_killed_preparation_is_reclaimed_on_restart(self):
        a = self.assignment('deepseek')
        package = Path(shutil.which('dsh')).resolve().parent.parent
        fake_bin = self.f.root / 'fake-bin'
        fake_bin.mkdir()
        node = fake_bin / 'node'
        marker = self.f.root / 'sdk-entered'
        # Read the real bridge request and pause after the transcript has been written.
        node.write_text("#!/usr/bin/env python3\nimport sys,json,pathlib,time\n"
                        "json.load(sys.stdin)\n"
                        f"pathlib.Path({str(marker)!r}).write_text('ready')\n"
                        "time.sleep(60)\n")
        node.chmod(0o700)
        args = [m1_cli.BIN, '--state-dir', self.f.root / 'state',
                '--codex-root', self.f.source_root, '--claude-root', self.f.root / 'claude',
                '--dsh-package', package, 'claim', a['id'], '--recipient', 'deepseek']
        env = {**os.environ, 'HOME': str(self.f.root),
               'PATH': str(fake_bin) + os.pathsep + os.environ['PATH']}
        process = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        observer = None
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), 'SDK request never reached pause point')
            self.assertEqual(len(self.orphans()), 1)
            observer = subprocess.Popen([m1_cli.BIN, '--state-dir', self.f.root / 'state', 'versions'],
                env={**os.environ, 'HOME': str(self.f.root)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(0.15)
            self.assertIsNone(observer.poll(), 'recovery must wait for active preparation')
            self.assertEqual(len(self.orphans()), 1)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
            if observer is not None:
                try:
                    stdout, stderr = observer.communicate(timeout=5)
                    self.assertEqual(observer.returncode, 0, stderr.decode())
                    self.assertIn('versions', json.loads(stdout))
                finally:
                    if observer.poll() is None:
                        observer.kill()
                        observer.communicate(timeout=5)
        # The next ordinary API operation performs recovery after acquiring the writer lock.
        inbox = self.server.call('list_assignments', {'recipient': 'deepseek'})['assignments']
        self.assertEqual(inbox[0]['status'], 'pending')
        self.assertEqual(self.orphans(), [])
        claimed = self.f.cli('claim', a['id'], '--recipient', 'deepseek')
        self.assertEqual(claimed['status'], 'waiting_for_open')
        self.assertEqual(self.orphans(), [])


if __name__ == '__main__':
    unittest.main()
