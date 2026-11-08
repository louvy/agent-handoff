"""Verify independent snapshots, source loss, branches and two native recipients."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
import m1_cli


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.f = m1_cli.CliTests()
        self.f.setUp()
        self.package = Path(shutil.which('dsh')).resolve().parent.parent
        self.root = self.f.root / 'deepseek'

    def tearDown(self):
        self.f.tearDown()

    def cli(self, *args, **kwargs):
        return self.f.cli('--dsh-root', self.root, '--dsh-package', self.package, *args, **kwargs)

    def test_two_recipients_after_source_and_notes_removed(self):
        v = self.cli('capture', 'source-id')
        self.f.source_root.rename(self.f.root / 'source-unavailable')
        self.f.note.unlink()
        plans = [self.cli('prepare', v['id'], '--to', target) for target in ('claude-desktop', 'deepseek')]
        for plan in plans:
            self.assertEqual(plan['version_id'], v['id'])
            result = self.cli('apply', plan['id'])
            self.assertEqual(result['status'], 'waiting_for_open')
            before = Path(result['target_path']).read_bytes()
            self.cli('apply', plan['id'])
            self.assertEqual(Path(result['target_path']).read_bytes(), before)
        claude = Path(plans[0]['target_path']).read_text()
        bridge = Path(__file__).resolve().parents[1] / 'adapters/deepseek.mjs'
        result = subprocess.run(['node', '--input-type=module', '-e', bridge.read_text(),
                                 str(self.package)], input=json.dumps({
                                     'root': str(self.root), 'op': 'read', 'id': plans[1]['target_id']}),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for text in (claude, result.stdout, self.cli('read', v['id'], '--section', 'transcript')['text']):
            for marker in ('history-only-97c3', 'array-output-A', 'array-output-B'):
                self.assertIn(marker, text)
        for text in (claude, result.stdout, self.cli('read', v['id'], '--section', 'memory')['text']):
            self.assertIn('only-in-memory-38a1', text)
        self.assertEqual(len(self.cli('versions', '--task-id', v['task_id'])['versions']), 1)

    def test_recapture_growth_and_recipient_branches(self):
        first = self.cli('capture', 'source-id')
        plan = self.cli('prepare', first['id'], '--to', 'claude-desktop')
        self.cli('apply', plan['id'])
        with self.f.source.open('a') as stream:
            stream.write(json.dumps({'type': 'response_item', 'payload': {'type': 'message',
                         'role': 'user', 'content': [{'type': 'input_text', 'text': 'new-source-work'}]}}) + '\n')
        second = self.cli('capture', 'source-id')
        self.assertEqual(second['task_id'], first['task_id'])
        self.assertEqual(second['parent_version_id'], first['id'])
        self.assertNotIn('new-source-work', self.cli('read', first['id'], '--section', 'transcript')['text'])
        self.assertIn('new-source-work', self.cli('read', second['id'], '--section', 'transcript')['text'])
        child = self.cli('capture', plan['target_id'], '--from', 'claude')
        self.assertEqual(child['task_id'], first['task_id'])
        self.assertEqual(child['parent_version_id'], first['id'])
        self.assertEqual(child['parent_plan_id'], plan['id'])

    def test_worktree_identity_still_required(self):
        v = self.cli('capture', 'source-id')
        plan = self.cli('prepare', v['id'], '--to', 'claude-desktop')
        self.f.source.unlink()
        self.f.work.rename(self.f.root / 'moved-worktree')
        self.assertIn('工作目录', self.cli('apply', plan['id'], ok=False))
        self.assertFalse(Path(plan['target_path']).exists())


if __name__ == '__main__':
    unittest.main()
