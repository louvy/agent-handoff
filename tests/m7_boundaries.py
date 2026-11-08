"""验证原生目标身份、发现阶段警告、事务回滚与进程中断恢复。"""
import concurrent.futures
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

    def test_builtin_delivered_history_must_survive_retry(self):
        for agent in ('codex', 'claude-desktop'):
            with self.subTest(agent=agent):
                plan = self.f.cli('preview', 'source-id', '--to', agent)
                self.f.cli('apply', plan['id'])
                target = Path(plan['target_path'])
                original = target.read_bytes()
                lines = original.decode().splitlines()
                self.assertGreater(len(lines), 2)
                # The surviving records still carry the target ID and cwd.
                target.write_text('\n'.join(lines[:2]) + '\n')
                self.assertEqual(json.loads(self.f.cli('apply', plan['id'], ok=False))['code'],
                                 'TARGET_INVALID')
                target.write_bytes(original)
                rewritten = original.replace(b'history-only-97c3', b'rewritten-history')
                self.assertNotEqual(rewritten, original)
                target.write_bytes(rewritten)
                self.assertEqual(json.loads(self.f.cli('apply', plan['id'], ok=False))['code'],
                                 'TARGET_INVALID')
                target.write_bytes(original)
                self.assertEqual(self.f.cli('apply', plan['id'])['target_id'], plan['target_id'])
                if agent == 'codex':
                    with target.open('a') as stream:
                        stream.write(json.dumps({
                            'type': 'response_item',
                            'timestamp': '2026-11-08T10:03:00Z',
                            'payload': {'type': 'message', 'role': 'user',
                                        'content': [{'type': 'input_text',
                                                     'text': 'new codex progress'}]},
                        }) + '\n')
                    self.assertEqual(self.f.cli('apply', plan['id'])['target_id'],
                                     plan['target_id'])
        with self.subTest(agent='deepseek'):
            plan = self.f.cli('preview', 'source-id', '--to', 'deepseek')
            self.f.cli('apply', plan['id'])
            target = Path(plan['target_path'])
            original = target.read_bytes()
            target.write_bytes(original[:len(original) // 2])
            self.assertEqual(json.loads(self.f.cli('apply', plan['id'], ok=False))['code'],
                             'TARGET_INVALID')
            target.write_bytes(original)
            self.assertEqual(self.f.cli('apply', plan['id'])['target_id'], plan['target_id'])

    def test_legacy_builtin_plan_uses_prepared_artifact(self):
        plan = self.f.cli('preview', 'source-id', '--to', 'claude-desktop')
        self.f.cli('apply', plan['id'])
        target = Path(plan['target_path'])
        original = target.read_bytes()
        database = self.f.root / 'state/handoff.sqlite3'
        with sqlite3.connect(database) as db:
            body, = db.execute('SELECT body FROM plans WHERE id=?', [plan['id']]).fetchone()
            value = json.loads(body)
            value.pop('history', None)
            value.pop('history_digest', None)
            db.execute('UPDATE plans SET body=? WHERE id=?', [json.dumps(value), plan['id']])
        (self.f.root / 'state' / plan['id'] / 'history.json').unlink()
        target.write_bytes(original.replace(b'history-only-97c3', b'rewritten-history'))
        self.assertEqual(json.loads(self.f.cli('apply', plan['id'], ok=False))['code'],
                         'TARGET_INVALID')
        target.write_bytes(original)
        self.assertEqual(self.f.cli('apply', plan['id'])['target_id'], plan['target_id'])

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

    def test_waiting_launcher_does_not_block_other_assignments(self):
        plan = self.f.preview()
        assignment = self.assignment('codex')
        marker = self.f.root / 'launcher-started'
        launcher = self.f.root / 'launcher'
        launcher.write_text("#!/usr/bin/env python3\nimport pathlib,time\n"
            f"pathlib.Path({str(marker)!r}).touch()\ntime.sleep(60)\n")
        launcher.chmod(0o700)
        env = {**os.environ, 'HOME': str(self.f.root), 'HANDOFF_CLAUDE_BIN': str(launcher)}
        base = [m1_cli.BIN, '--state-dir', self.f.root / 'state',
                '--codex-root', self.f.source_root, '--claude-root', self.f.root / 'claude']
        opening = subprocess.Popen([*base, 'apply', plan['id'], '--open'], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        claim = None
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline and opening.poll() is None:
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            self.assertEqual(self.f.cli('status', plan['id'])['status'], 'waiting_for_open')
            claim = subprocess.Popen([*base, 'claim', assignment['id'], '--recipient', 'codex'],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = claim.communicate(timeout=3)
            self.assertEqual(claim.returncode, 0, stderr.decode())
            self.assertEqual(json.loads(stdout)['status'], 'waiting_for_open')
            retry = self.server.call('claim_assignment',
                {'assignment_id': assignment['id'], 'recipient': 'codex'})
            self.assertEqual(retry['plan']['target_id'], json.loads(stdout)['plan']['target_id'])
            self.assertIsNone(opening.poll())
        finally:
            if opening.poll() is None:
                os.killpg(opening.pid, signal.SIGKILL)
            opening.communicate(timeout=5)
            if claim is not None:
                if claim.poll() is None:
                    claim.kill()
                claim.communicate(timeout=5)

    def assert_process_gone(self, pid):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        self.fail(f'Adapter process {pid} remains alive')

    def test_adapter_is_reaped_when_hub_exits(self):
        package = Path(shutil.which('dsh')).resolve().parent.parent
        fake_bin = self.f.root / 'cancel-bin'
        fake_bin.mkdir()
        marker = self.f.root / 'cancel-sdk-pid'
        helper_marker = self.f.root / 'cancel-helper-pid'
        node = fake_bin / 'node'
        node.write_text("#!/usr/bin/env python3\nimport os,pathlib,subprocess,sys,time\n"
            "helper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"pathlib.Path({str(helper_marker)!r}).write_text(str(helper.pid))\n"
            f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\ntime.sleep(60)\n")
        node.chmod(0o700)
        env = {**os.environ, 'HOME': str(self.f.root),
               'PATH': str(fake_bin) + os.pathsep + os.environ['PATH']}
        # SIGKILL cannot be handled by the hub; neither cancellation mode may
        # leave a detached SDK running after the operation lock is released.
        for death_signal in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=death_signal):
                marker.unlink(missing_ok=True)
                assignment = self.assignment('deepseek')
                process = subprocess.Popen([m1_cli.BIN, '--state-dir', self.f.root / 'state',
                    '--dsh-package', package, '--adapter-timeout-seconds', '1',
                    'claim', assignment['id'], '--recipient', 'deepseek'], env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                sdk = None
                try:
                    deadline = time.monotonic() + 5
                    while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
                        time.sleep(0.02)
                    self.assertTrue(marker.exists())
                    sdk = int(marker.read_text())
                    os.killpg(process.pid, death_signal)
                    process.communicate(timeout=3)
                    self.assert_process_gone(sdk)
                    self.assert_process_gone(int(helper_marker.read_text()))
                    self.assertEqual(self.f.cli('inbox', '--recipient', 'deepseek')['assignments'][0]['status'], 'pending')
                    self.assertEqual(self.orphans(), [])
                    delivered = self.f.cli('claim', assignment['id'], '--recipient', 'deepseek')
                    retry = self.f.cli('claim', assignment['id'], '--recipient', 'deepseek')
                    self.assertEqual(delivered['plan']['target_id'], retry['plan']['target_id'])
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=3)
                    if sdk is not None:
                        try:
                            os.killpg(sdk, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_adapter_timeout_reaps_child_and_assignment_can_retry(self):
        # A large request and an SDK that never reads stdin exercise pipe backpressure.
        self.f.source.write_text(self.f.source.read_text().replace('history-only-97c3', 'x' * 200000))
        a = self.assignment('deepseek')
        package = Path(shutil.which('dsh')).resolve().parent.parent
        fake_bin = self.f.root / 'timeout-bin'
        fake_bin.mkdir()
        marker = self.f.root / 'adapter-pid'
        helper_marker = self.f.root / 'timeout-helper-pid'
        node = fake_bin / 'node'
        node.write_text("#!/usr/bin/env python3\nimport os,pathlib,subprocess,sys,time\n"
                        "helper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                        f"pathlib.Path({str(helper_marker)!r}).write_text(str(helper.pid))\n"
                        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
                        "sys.stderr.write('diagnostic' * 20000)\nsys.stderr.flush()\ntime.sleep(60)\n")
        node.chmod(0o700)
        env = {**os.environ, 'HOME': str(self.f.root),
               'PATH': str(fake_bin) + os.pathsep + os.environ['PATH']}
        result = subprocess.run([m1_cli.BIN, '--state-dir', self.f.root / 'state',
            '--dsh-package', package, '--adapter-timeout-seconds', '1',
            'claim', a['id'], '--recipient', 'deepseek'], env=env,
            capture_output=True, text=True, timeout=6)
        self.assertNotEqual(result.returncode, 0)
        issue = json.loads(result.stderr)
        self.assertEqual(issue['code'], 'ADAPTER_TIMEOUT')
        self.assertTrue(issue['retryable'])
        self.assertTrue(marker.exists())
        self.assert_process_gone(int(marker.read_text()))
        self.assert_process_gone(int(helper_marker.read_text()))
        self.assertEqual(self.orphans(), [])
        self.assertEqual(self.f.cli('inbox', '--recipient', 'deepseek')['assignments'][0]['status'], 'pending')
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(lambda _: self.f.cli('claim', a['id'], '--recipient', 'deepseek'), range(2)))
        self.assertEqual(claims[0]['plan']['target_id'], claims[1]['plan']['target_id'])
        self.assertEqual(claims[0]['status'], 'waiting_for_open')
        self.assertTrue(Path(claims[0]['plan']['target_path']).is_file())
        with sqlite3.connect(self.f.root / 'state/handoff.sqlite3') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM plans').fetchone()[0], 1)
        # A stalled validation of an already delivered target must also time out
        # without replacing the native session or losing its plan identity.
        plan = claims[0]['plan']
        original = Path(plan['target_path']).read_bytes()
        result = subprocess.run([m1_cli.BIN, '--state-dir', self.f.root / 'state',
            '--dsh-package', package, '--adapter-timeout-seconds', '1',
            'apply', plan['id']], env=env, capture_output=True, text=True, timeout=6)
        self.assertEqual(json.loads(result.stderr)['code'], 'ADAPTER_TIMEOUT')
        self.assertEqual(Path(plan['target_path']).read_bytes(), original)
        self.assertEqual(self.f.cli('apply', plan['id'])['target_id'], plan['target_id'])

    def test_killed_preparation_is_reclaimed_on_restart(self):
        a = self.assignment('deepseek')
        published = self.f.preview()
        self.f.cli('apply', published['id'])
        package = Path(shutil.which('dsh')).resolve().parent.parent
        fake_bin = self.f.root / 'fake-bin'
        fake_bin.mkdir()
        node = fake_bin / 'node'
        marker = self.f.root / 'sdk-entered'
        # Read the real bridge request and pause after the transcript has been written.
        node.write_text("#!/usr/bin/env python3\nimport os,sys,json,pathlib,time\n"
                        "json.load(sys.stdin)\n"
                        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
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
            stdout, stderr = observer.communicate(timeout=3)
            self.assertEqual(observer.returncode, 0, stderr.decode())
            self.assertIn('versions', json.loads(stdout))
            self.assertEqual(self.server.call('list_assignments', {'recipient': 'deepseek'})['assignments'][0]['status'], 'pending')
            self.assertEqual(self.f.cli('status', published['id'])['status'], 'waiting_for_open')
            self.assertEqual(self.server.call('get_handoff', {'handoff_id': published['id']})['status'], 'waiting_for_open')
            self.assertEqual(len(self.orphans()), 1)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
            # The adapter has its own process group, separate from the killed hub.
            if marker.exists():
                try:
                    os.killpg(int(marker.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if observer is not None:
                try:
                    stdout, stderr = observer.communicate(timeout=5)
                    self.assertEqual(observer.returncode, 0, stderr.decode())
                    self.assertIn('versions', json.loads(stdout))
                finally:
                    if observer.poll() is None:
                        observer.kill()
                        observer.communicate(timeout=5)
        # Once the operation lock is released, the next API call reclaims preparation.
        inbox = self.server.call('list_assignments', {'recipient': 'deepseek'})['assignments']
        self.assertEqual(inbox[0]['status'], 'pending')
        self.assertEqual(self.orphans(), [])
        claimed = self.f.cli('claim', a['id'], '--recipient', 'deepseek')
        self.assertEqual(claimed['status'], 'waiting_for_open')
        self.assertEqual(self.orphans(), [])


if __name__ == '__main__':
    unittest.main()
