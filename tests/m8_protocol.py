"""An independent local client joins through public CLI/MCP and resumes in a worktree."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import m1_cli
import m2_mcp


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.server = m2_mcp.McpTests()
        self.server.setUp()
        self.addCleanup(self.server.tearDown)
        self.f = self.server.fixture
        self.call = self.server.call

    def register(self, agent, role='both', **kwargs):
        return self.call('register_agent', {'protocol_version': 1, 'id': agent, 'role': role}, **kwargs)

    def test_external_agent_roundtrip(self):
        f = self.f
        # This consumer runs outside the repo and uses only stdlib, CLI and protocol files.
        client = f.root / 'consumer.py'
        client.write_text("""
import json, os, subprocess, sys
from pathlib import Path
from datetime import datetime, timezone
def hub(*args):
    return json.loads(subprocess.check_output([sys.argv[1], *args]))
registration = hub('register-agent', '--id', 'example-agent', '--role', 'both', '--protocol-version', '1')
if len(sys.argv) > 2:
    assignment, = hub('inbox', '--recipient', 'example-agent')['assignments']
    plan = hub('claim', assignment['id'], '--recipient', 'example-agent')['plan']
    path = Path(registration['sessions_dir']) / (plan['target_id'] + '.json')
    session = json.loads(path.read_text())
    os.chdir(session['meta']['cwd'])
    branch = subprocess.check_output(['git', 'branch', '--show-current'], text=True).strip()
    marker = Path('marker.txt').read_text()
    session['messages'].append({'role':'assistant','timestamp':datetime.now(timezone.utc).isoformat(),
        'content':[{'type':'text','text':f'continued on {branch}: {marker}'}]})
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(session))
    tmp.replace(path)
    print(json.dumps(plan))
else:
    print(json.dumps(registration))
""")
        def run_client(*args):
            env = {**os.environ, 'HANDOFF_STATE_DIR': str(f.root / 'state')}
            return json.loads(subprocess.check_output(
                [sys.executable, client, m1_cli.BIN, *args], cwd=f.root, env=env))

        registration = run_client()
        self.assertIn(registration, self.call('list_agents', {})['agents'])
        version = self.call('capture_version', {'source_ref': 'source-id', 'source_agent': 'codex'})
        assignment = self.call('assign_version', {'version_id': version['id'],
                               'recipient': 'example-agent', 'instructions': 'check local marker'})
        plan = run_client('claim')
        target = Path(plan['target_path'])
        self.assertEqual(target.parent, Path(registration['sessions_dir']))
        session = json.loads(target.read_text())
        self.assertEqual(session['meta']['id'], plan['target_id'])
        body = json.dumps(session['messages'])
        for expected in ['history-only-97c3', 'array-output-A', 'array-output-B',
                         'check local marker', 'only-in-memory-38a1', 'continued on target: target']:
            self.assertIn(expected, body)
        self.assertEqual(f.cli('claim', assignment['id'], '--recipient', 'example-agent', '--open')
                         ['plan']['open_issue']['code'], 'MANUAL_OPEN_REQUIRED')
        self.assertEqual(self.call('claim_assignment', {'assignment_id': assignment['id'],
                         'recipient': 'example-agent'})['plan']['target_id'], plan['target_id'])
        self.assertEqual(len(f.cli('versions')['versions']), 1)
        found = self.call('find_sessions', {'agent': 'example-agent', 'query': 'continued on target'})
        self.assertEqual(found['sessions'][0]['source_ref'], str(target))
        back = self.call('preview_handoff', {'source_agent': 'example-agent',
                         'source_ref': plan['target_id'], 'target': 'claude-desktop'})
        self.assertEqual(back['parent_plan_id'], plan['id'])
        self.assertEqual(back['task_id'], plan['task_id'])
        delivered = f.cli('apply', back['id'])
        self.assertEqual(delivered['workspace']['cwd'], str(f.work))
        self.assertIn('continued on target: target', Path(delivered['target_path']).read_text())

    def test_roles_collisions_versions_and_offline_retry(self):
        f = self.f
        self.assertEqual(self.register('codex', ok=False)['code'], 'INVALID_REGISTRATION')
        self.assertEqual(self.register('../escape', ok=False)['code'], 'INVALID_REGISTRATION')
        self.assertEqual(self.call('register_agent', {'protocol_version': 2, 'id': 'vnext',
                         'role': 'both'}, ok=False)['code'], 'PROTOCOL_UNSUPPORTED')
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            regs = list(pool.map(lambda _: f.cli('register-agent', '--id', 'reader',
                             '--role', 'target', '--protocol-version', '1'), range(4)))
        self.assertTrue(all(r == regs[0] for r in regs))
        self.assertEqual(self.register('reader', 'source', ok=False)['code'], 'REGISTRATION_CONFLICT')
        self.register('writer', 'source')
        self.assertEqual(self.call('find_sessions', {'agent':'reader'}, ok=False)['code'],
                         'CAPABILITY_UNSUPPORTED')
        self.assertEqual(self.call('list_assignments', {'recipient':'writer'}, ok=False)['code'],
                         'CAPABILITY_UNSUPPORTED')
        self.assertEqual(self.call('find_sessions', {'agent':'unregistered'}, ok=False)['code'],
                         'AGENT_UNKNOWN')
        version = f.cli('capture', 'source-id')
        self.assertEqual(json.loads(f.cli('prepare', version['id'], '--to', 'writer', ok=False))
                         ['code'], 'CAPABILITY_UNSUPPORTED')
        path = Path(regs[0]['sessions_dir'])
        path.rmdir()
        assignment = f.cli('assign', version['id'], '--to', 'reader', '--instructions', 'resume')
        self.assertTrue(json.loads(f.cli('claim', assignment['id'], '--recipient', 'reader',
                        ok=False))['retryable'])
        self.register('reader','target')
        plan = f.cli('claim', assignment['id'], '--recipient', 'reader')['plan']
        self.assertEqual(json.loads(Path(plan['target_path']).read_text())['meta']['id'],plan['target_id'])

    def test_source_format_workspace_and_link_boundaries(self):
        f = self.f
        store = Path(self.register('source-agent','source')['sessions_dir'])
        dest = Path(self.register('target-agent','target')['sessions_dir'])
        path = store / 'local.json'
        stamp = '2026-01-01T00:00:00Z'
        session = {'protocol_version':1, 'meta':{'id':'local','timestamp':stamp},
                   'messages':[{'role':'user','timestamp':stamp,
                                'content':[{'type':'text','text':'portable session'}]}]}
        path.write_text(json.dumps(session))
        def capture_error():
            return self.call('capture_version', {'source_agent':'source-agent',
                             'source_ref':str(path)}, ok=False)['code']
        self.assertEqual(capture_error(), 'WORKSPACE_UNSPECIFIED')
        session['meta']['cwd'] = str(f.work)
        session['meta']['id'] = 'wrong-id'
        path.write_text(json.dumps(session))
        self.assertEqual(capture_error(), 'SOURCE_INVALID')
        self.assertEqual(json.loads(f.cli('capture', 'local', '--from', 'source-agent', ok=False))['code'], 'SOURCE_INVALID')
        session['meta']['id'] = 'local'
        path.write_text(json.dumps(session))
        (store / 'link.json').symlink_to(path)
        listing = f.cli('sessions','--agent','source-agent')
        self.assertTrue(listing['partial'])
        self.assertEqual(listing['warnings'][0]['issue']['code'], 'PROTOCOL_PATH_INVALID')
        self.assertEqual([s['id'] for s in listing['sessions']], ['local'])
        plan = f.cli('preview', 'local','--from','source-agent','--to','target-agent')
        dest.rmdir()
        dest.symlink_to(f.control)
        self.assertEqual(json.loads(f.cli('apply',plan['id'],ok=False))['code'],'PROTOCOL_PATH_INVALID')
        self.assertFalse((f.control / (plan['target_id'] + '.json')).exists())
        dest.unlink()
        dest.mkdir()
        # Simulate a crash between atomic publication and the SQLite status update.
        target = Path(plan['target_path'])
        expected = (f.root / 'state' / plan['id'] / 'native.jsonl').read_text()
        existing = json.loads(expected)
        existing['messages'][0]['content'][0]['text'] = 'unrelated history'
        target.write_text(json.dumps(existing))
        self.assertEqual(json.loads(f.cli('apply',plan['id'],ok=False))['code'], 'TARGET_CONFLICT')
        existing = json.loads(expected)
        existing['messages'].append({'role':'assistant','timestamp':stamp,
                                     'content':[{'type':'text','text':'work after publication'}]})
        target.write_text(json.dumps(existing, indent=2))
        published = f.cli('apply',plan['id'])
        self.assertEqual(json.loads(target.read_text()), existing)
        self.assertEqual(json.loads(Path(published['target_path']).read_text())['meta']['cwd'],str(f.work))
