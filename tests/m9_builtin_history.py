"""Integrity of delivered native sessions, including plans saved before history manifests."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

import m1_cli
import m2_mcp


def native_rows(target):
    data = (subprocess.check_output(['zstd', '-qdc', target])
            if target.suffix == '.zstd' else target.read_bytes())
    return [json.loads(line) for line in data.splitlines()]


def save_rows(target, rows):
    if target.suffix != '.zstd':
        # Reserializing also changes whitespace and object key order.
        target.write_text(''.join(json.dumps(r, sort_keys=True) + '\n' for r in rows))
        return
    # The SDK validates frame boundaries, event sequences and their references.
    # Regenerate a real SDK store, then use that file as the client-edited target.
    package = Path(shutil.which('dsh')).resolve().parent.parent
    script = """
import {createRequire} from 'node:module';
const req = createRequire(process.argv[1] + '/package.json');
const {Context} = await import(req.resolve('@deepseek-ai/cordis'));
const backend = await import(req.resolve('@deepseek-ai/dsh-session-persistence-jsonl'));
let input = '';
for await (const chunk of process.stdin) input += chunk;
const [{type, ...header}, ...events] = JSON.parse(input);
const ctx = new Context();
const scope = await ctx.plugin(backend, {root: process.argv[2]});
try {
    const handle = await ctx.sessionPersistence.create(header);
    try { await handle.append(events); await handle.flush(); }
    finally { await handle.close(); }
    console.log((await ctx.sessionPersistence.stat(header.id)).location.path);
} finally { await scope.dispose(); }
"""
    with tempfile.TemporaryDirectory() as root:
        file = subprocess.check_output(['node', '--input-type=module', '-e', script,
                                        package, root], input=json.dumps(rows), text=True).strip()
        target.write_bytes(Path(file).read_bytes())


def continue_session(target, agent):
    rows = native_rows(target)
    marker = 'continued-in-original-worktree'
    if agent == 'codex':
        rows.append({'type': 'response_item', 'timestamp': '2026-01-01T00:00:00Z',
                     'payload': {'type': 'message', 'role': 'user',
                                'content': [{'type': 'input_text', 'text': marker}]}})
    elif agent == 'claude-desktop':
        rows[-1]['customTitle'] = 'Renamed locally'
        last = next(r for r in reversed(rows) if r.get('type') == 'user')
        rows.append({**last, 'uuid': str(uuid.uuid4()), 'parentUuid':last['uuid'],
                     'message': {'role': 'user', 'content': marker}})
    else:
        title = next(r for r in rows if r['type'] == 'session/title')
        title['data']['title'] = 'Renamed locally'
        user = next(r for r in rows if r['type'] == 'user/message')
        rows.append({**user, 'seq': rows[-1]['seq'] + 1, 'data': {**user['data'],
                     'id': str(uuid.uuid4()), 'content': [{'type': 'text', 'text': marker}]}})
    save_rows(target, rows)
    return marker


class BuiltinHistoryTests(unittest.TestCase):
    def setUp(self):
        self.server = m2_mcp.McpTests()
        self.server.setUp()
        self.addCleanup(self.server.tearDown)
        self.f = self.server.fixture

    def delivered(self, agent):
        version = self.f.cli('capture', 'source-id')
        assignment = self.f.cli('assign', version['id'], '--to', agent,
                                '--instructions', 'specific assignment instructions')
        plan = self.f.cli('claim', assignment['id'], '--recipient', agent)['plan']
        return assignment, plan

    def check_history(self, agent, legacy):
        # An optional older checkout can produce real pre-upgrade artifacts.
        legacy_bin = os.environ.get('HANDOFF_LEGACY_BINARY') if legacy else None
        if legacy_bin:
            with patch.object(m1_cli, 'BIN', Path(legacy_bin).resolve()):
                assignment, plan = self.delivered(agent)
        else:
            assignment, plan = self.delivered(agent)
        state = self.f.root / 'state'
        folder = state / plan['id']
        if legacy:
            with sqlite3.connect(state / 'handoff.sqlite3') as db:
                row = json.loads(db.execute('SELECT body FROM plans WHERE id=?', [plan['id']]).fetchone()[0])
                if legacy_bin:
                    self.assertNotIn('history', row)
                    self.assertNotIn('history_digest', row)
                else:
                    del row['history'], row['history_digest']
                    db.execute('UPDATE plans SET body=? WHERE id=?', [json.dumps(row), plan['id']])
            (folder / 'history.json').unlink(missing_ok=True)
            if (folder / 'staging').exists():
                shutil.rmtree(folder / 'staging')

        target = Path(plan['target_path'])
        original = target.read_bytes()
        source = 'claude' if agent == 'claude-desktop' else agent

        def read_target():
            captured = self.f.cli('capture', plan['target_id'], '--from', source)
            self.assertEqual(captured['workspace']['cwd'], str(self.f.work))
            return self.f.cli('read', captured['id'], '--section', 'transcript')['text']

        def check_retry(code=None):
            before = target.read_bytes()
            args = {'recipient': agent, 'assignment_id': assignment['id']}
            if code:
                self.assertEqual(json.loads(self.f.cli('apply', plan['id'], ok=False))['code'], code)
                self.assertEqual(self.server.call('claim_assignment', args, ok=False)['code'], code)
            else:
                applied = self.f.cli('apply', plan['id'])
                claimed = self.server.call('claim_assignment', args)
                self.assertEqual(claimed['id'], assignment['id'])
                for result in (applied, claimed['plan']):
                    self.assertEqual((result['id'], result['target_id'], result['status']),
                                     (plan['id'], plan['target_id'], 'waiting_for_open'))
            self.assertEqual(target.read_bytes(), before)

        for removed, replacement in [('array-output-A', 'changed-result'),
                                    ('specific assignment instructions', '')]:
            rows = native_rows(target)
            changed = json.dumps(rows).replace(removed, replacement)
            self.assertNotIn(removed, changed)
            save_rows(target, json.loads(changed))
            transcript = read_target()
            self.assertNotIn(removed, transcript)
            self.assertIn(replacement or 'only-in-memory-38a1', transcript)
            check_retry('TARGET_INVALID')
            target.write_bytes(original)
            check_retry()

        # Remove a complete middle assistant turn, keeping the remaining native log valid.
        rows = native_rows(target)
        kept = [r for r in rows if '"Ready"' not in json.dumps(r)]
        self.assertLess(len(kept), len(rows))
        if agent == 'claude-desktop':
            removed = next(r for r in rows if r not in kept)
            for row in kept:
                if row.get('parentUuid') == removed['uuid']:
                    row['parentUuid'] = removed['parentUuid']
        if agent == 'deepseek':
            for seq, event in enumerate(kept[1:]):
                event['seq'] = seq
        save_rows(target, kept)
        self.assertNotIn('Ready', read_target())
        check_retry('TARGET_INVALID')
        target.write_bytes(original)
        marker = continue_session(target, agent)
        self.assertIn(marker, read_target())
        check_retry()
        # Recover after the file was published but before the status transaction committed.
        with sqlite3.connect(state / 'handoff.sqlite3') as db:
            db.execute("UPDATE plans SET status='previewed' WHERE id=?", [plan['id']])
        check_retry()
        if not legacy:
            with sqlite3.connect(state / 'handoff.sqlite3') as db:
                stored = db.execute('SELECT body FROM plans WHERE id=?', [plan['id']]).fetchone()[0]
                incomplete = json.loads(stored)
                del incomplete['history']
                db.execute('UPDATE plans SET body=? WHERE id=?', [json.dumps(incomplete), plan['id']])
            check_retry('PLAN_CORRUPT')
            with sqlite3.connect(state / 'handoff.sqlite3') as db:
                db.execute('UPDATE plans SET body=? WHERE id=?', [stored, plan['id']])
        baseline = folder / ('native.jsonl' if legacy else 'history.json')
        pristine = baseline.read_bytes()
        baseline.unlink()
        check_retry('BASELINE_MISSING')
        baseline.write_bytes(pristine + b' ')
        check_retry('PLAN_CORRUPT')
        baseline.write_bytes(pristine)
        baseline.chmod(0)
        try:
            check_retry('ACCESS_DENIED')
        finally:
            baseline.chmod(0o600)
        check_retry()

    def test_new_and_legacy_plans(self):
        for agent in ('codex', 'claude-desktop', 'deepseek'):
            for legacy in (False, True):
                with self.subTest(agent=agent, legacy=legacy):
                    self.check_history(agent, legacy)
