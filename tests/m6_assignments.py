"""Verify durable assignment delivery, concurrent claims and interrupted retries."""
import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
import m1_cli


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        self.f = m1_cli.CliTests()
        self.f.setUp()

    def tearDown(self):
        self.f.tearDown()

    def assign(self):
        v = self.f.cli('capture', 'source-id')
        a = self.f.cli('assign', v['id'], '--to', 'claude-desktop',
                       '--instructions', 'M6-request-97df: add the empty-list regression')
        self.assertEqual(a['status'], 'pending')
        self.assertIsNone(a['plan'])
        self.f.source_root.rename(self.f.root / 'held-source')
        self.f.note.unlink()
        return v, a

    def test_offline_assignment_and_concurrent_claims(self):
        version, assignment = self.assign()
        pending = self.f.cli('inbox', '--recipient', 'claude-desktop')['assignments']
        self.assertEqual([a['id'] for a in pending], [assignment['id']])
        self.assertEqual(self.f.cli('inbox', '--recipient', 'deepseek')['assignments'], [])
        self.f.cli('claim', assignment['id'], '--recipient', 'deepseek', ok=False)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            claims = list(executor.map(
                lambda _: self.f.cli('claim', assignment['id'], '--recipient', 'claude-desktop'),
                range(4)))
        self.assertEqual(len({a['plan']['target_id'] for a in claims}), 1)
        plan = claims[0]['plan']
        self.assertEqual(plan['version_id'], version['id'])
        text = Path(plan['target_path']).read_text()
        for marker in ('M6-request-97df', 'history-only-97c3', 'only-in-memory-38a1', 'array-output-A'):
            self.assertIn(marker, text)
        self.assertNotIn('M6-request-97df', self.f.cli('read', version['id'],
                                                   '--section', 'transcript')['text'])
        with sqlite3.connect(self.f.root / 'state/handoff.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM plans').fetchone()[0], 1)

    def test_each_recipient_restores_same_version_with_own_instructions(self):
        version, _ = self.assign()
        for recipient in ('codex', 'claude-desktop', 'deepseek'):
            with self.subTest(recipient=recipient):
                a = self.f.cli('assign', version['id'], '--to', recipient,
                               '--instructions', 'M6-deliver-' + recipient)
                claimed = self.f.cli('--dsh-root', self.f.root / 'dsh',
                                     'claim', a['id'], '--recipient', recipient)
                path = Path(claimed['plan']['target_path'])
                if recipient == 'deepseek':
                    import subprocess
                    text = subprocess.check_output(['zstd', '-dc', path]).decode()
                else:
                    text = path.read_text()
                self.assertEqual(claimed['plan']['version_id'], version['id'])
                self.assertIn('M6-deliver-' + recipient, text)
                self.assertIn('history-only-97c3', text)

    def test_failed_publish_and_open_resume_same_plan(self):
        _, assignment = self.assign()
        moved = self.f.root / 'moved-work'
        self.f.work.rename(moved)
        self.f.cli('claim', assignment['id'], '--recipient', 'claude-desktop', ok=False)
        pending = self.f.cli('inbox', '--recipient', 'claude-desktop')['assignments'][0]
        self.assertEqual(pending['status'], 'previewed')
        target_id = pending['plan']['target_id']
        moved.rename(self.f.work)
        with patch.dict(os.environ, {'HANDOFF_CLAUDE_BIN': 'false'}):
            claimed = self.f.cli('claim', assignment['id'], '--recipient', 'claude-desktop', '--open')
        self.assertIn('open_error', claimed['plan'])
        self.assertEqual(claimed['status'], 'waiting_for_open')
        self.assertEqual(claimed['plan']['target_id'], target_id)
        target = Path(claimed['plan']['target_path'])
        target.write_text(target.read_text() + '{"type":"m6-native-new-event"}\n')
        before = target.read_bytes()
        retried = self.f.cli('claim', assignment['id'], '--recipient', 'claude-desktop')
        self.assertEqual(retried['plan']['target_id'], target_id)
        self.assertEqual(target.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
