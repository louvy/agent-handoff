"""CLI regression checks using isolated Git worktrees and native Codex records."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

BIN = Path(__file__).resolve().parents[1] / 'target/debug/handoff'


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.control = self.root / 'control'
        self.control.mkdir()
        self.git(self.control, 'init', '-qb', 'control')
        (self.control / 'marker.txt').write_text('control')
        self.git(self.control, 'add', '.')
        self.git(self.control, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-qm', '建立测试')
        self.work = self.root / '工作 tree'
        self.git(self.control, 'worktree', 'add', '-qb', 'target', str(self.work))
        (self.work / 'marker.txt').write_text('target')
        self.git(self.work, 'add', '.')
        self.source_root = self.root / 'codex'
        self.source_root.mkdir()
        self.source = self.source_root / 'rollout-source.jsonl'
        self.write_source(self.work)
        self.note = self.root / 'memory.md'
        self.note.write_text('only-in-memory-38a1')
        self.cli('memory', 'bind', '--project', str(self.work), '--path', str(self.note))

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def git(cwd, *args):
        return subprocess.check_output(['git', '-C', str(cwd), *args], stderr=subprocess.PIPE)

    def write_source(self, cwd):
        rows = [
            {'type': 'session_meta', 'payload': {'id': 'source-id', 'cwd': str(cwd),
             'timestamp': '2026-11-08T10:00:00Z'}},
            {'type': 'response_item', 'timestamp': '2026-11-08T10:00:00Z',
             'payload': {'type': 'message', 'role': 'user',
                         'content': [{'type': 'input_text', 'text': 'history-only-97c3'}]}},
            {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'call_id': 'call-test', 'name': 'exec', 'input': 'run test'}},
            {'type': 'response_item', 'payload': {'type': 'custom_tool_call_output', 'call_id': 'call-test', 'output': [{'type': 'input_text', 'text': 'array-output-A'}, {'type': 'input_text', 'text': 'array-output-B'}]}},
            {'type': 'response_item', 'timestamp': '2026-11-08T10:00:01Z',
             'payload': {'type': 'message', 'role': 'assistant',
                         'content': [{'type': 'output_text', 'text': 'Ready'}]}},
        ]
        self.source.write_text(''.join(json.dumps(r) + '\n' for r in rows))

    def cli(self, *args, ok=True):
        p = subprocess.run([BIN, '--state-dir', self.root / 'state', '--codex-root',
                            self.source_root, '--claude-root', self.root / 'claude', *args],
                           capture_output=True, text=True, env={**os.environ, "HOME": str(self.root)})
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)
        self.assertNotEqual(p.returncode, 0)
        return p.stderr

    def preview(self):
        return self.cli('preview', 'source-id')

    def test_native_history_memory_and_concurrent_retry(self):
        plan = self.preview()
        self.assertEqual(plan['workspace']['root'], str(self.work))
        self.assertFalse(Path(plan['target_path']).exists())
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.cli('apply', plan['id']), range(2)))
        self.assertEqual(results[0]['target_id'], results[1]['target_id'])
        p = subprocess.run([BIN, '--state-dir', self.root / 'state', 'status', plan['id']], cwd=self.control, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        target = Path(plan['target_path'])
        body = target.read_text()
        self.assertIn('history-only-97c3', body)
        self.assertIn('only-in-memory-38a1', body)
        self.assertIn('array-output-A', body)
        self.assertIn('array-output-B', body)
        self.assertIn('custom-title', body)
        self.assertIn('Handoff ' + plan['id'][:8], body)
        self.assertIn('工作 tree', body)
        self.assertEqual(self.git(self.work, 'status', '--porcelain'), b'M  marker.txt\n')
        # A retry must preserve changes subsequently appended by the actual client.
        with target.open('a') as f:
            f.write('{"client":"continued"}\n')
        self.cli('apply', plan['id'])
        self.assertTrue(target.read_text().endswith('{"client":"continued"}\n'))

    def test_changed_source_and_note_rejected(self):
        plan = self.preview()
        with self.source.open('a') as f:
            f.write('\n')
        self.assertIn('来源已变化', self.cli('apply', plan['id'], ok=False))
        plan = self.preview()
        self.note.write_text('changed')
        self.assertIn('笔记已变化', self.cli('apply', plan['id'], ok=False))
        self.assertFalse(Path(plan['target_path']).exists())

    def test_missing_directory_does_not_fall_back(self):
        self.write_source(self.root / 'missing')
        self.assertIn('来源工作目录已不存在', self.cli('preview', 'source-id', ok=False))

    def test_collision_does_not_overwrite(self):
        plan = self.preview()
        target = Path(plan['target_path'])
        target.parent.mkdir(parents=True)
        target.write_text('existing unrelated session')
        self.cli('apply', plan['id'], ok=False)
        self.assertEqual(target.read_text(), 'existing unrelated session')

    def test_partial_publication_and_database_recovery(self):
        import sqlite3
        plan = self.preview()
        target = Path(plan['target_path'])
        target.parent.mkdir(parents=True)
        target.with_suffix('.handoff-tmp').write_text('interrupted partial bytes')
        self.cli('apply', plan['id'])
        original = target.read_bytes()
        # Simulate publication followed by a crash before the database commit.
        with sqlite3.connect(self.root / 'state/handoff.sqlite3') as db:
            db.execute("UPDATE plans SET status='previewed' WHERE id=?", [plan['id']])
        self.cli('apply', plan['id'])
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse(target.with_suffix('.handoff-tmp').exists())

    def test_detached_head_and_subdirectory(self):
        self.git(self.work, 'checkout', '--detach')
        subdir = self.work / 'src'
        subdir.mkdir()
        self.write_source(subdir)
        plan = self.preview()
        self.assertEqual(plan['workspace']['cwd'], str(subdir))
        self.assertEqual(plan['workspace']['root'], str(self.work))
        self.cli('apply', plan['id'])

    def test_open_failure_keeps_same_target(self):
        plan = self.preview()
        prior = os.environ.get('HANDOFF_CLAUDE_BIN')
        os.environ['HANDOFF_CLAUDE_BIN'] = str(self.root / 'missing-claude')
        try:
            result = self.cli('apply', plan['id'], '--open')
        finally:
            if prior is None:
                del os.environ['HANDOFF_CLAUDE_BIN']
            else:
                os.environ['HANDOFF_CLAUDE_BIN'] = prior
        self.assertEqual(result['status'], 'waiting_for_open')
        self.assertIn('open_error', result)
        self.assertEqual(self.cli('apply', plan['id'])['target_id'], result['target_id'])


if __name__ == '__main__':
    unittest.main()
