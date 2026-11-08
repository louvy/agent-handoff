"""Exercise the actual stdio server, bidirectional conversion, and CLI interoperability."""
import json
import os
import queue
import threading
import subprocess
import unittest
from pathlib import Path
import m1_cli


class McpTests(unittest.TestCase):
    def setUp(self):
        self.fixture = m1_cli.CliTests()
        self.fixture.setUp()
        f = self.fixture
        self.process = subprocess.Popen(
            [m1_cli.BIN, '--state-dir', f.root / 'state', '--codex-root', f.source_root,
             '--claude-root', f.root / 'claude', *getattr(self, 'extra_args', []), 'mcp'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env={**os.environ, 'HOME': str(f.root)})
        self.responses = queue.Queue()
        def collect():
            for line in self.process.stdout:
                try:
                    self.responses.put(json.loads(line))
                except json.JSONDecodeError as error:
                    self.responses.put(error)
            self.responses.put(None)
        self.reader = threading.Thread(target=collect, daemon=True)
        self.reader.start()
        self.sequence = 0
        self.pending = {}
        self.send('initialize', {'protocolVersion': '2025-06-18',
                                'capabilities': {}, 'clientInfo': {'name': 'm2-test', 'version': '1'}})
        initialized = self.receive(1)['result']
        self.assertEqual(initialized['serverInfo']['name'], 'agent-handoff')
        version = subprocess.check_output([m1_cli.BIN, '--version'], text=True).split()[-1]
        self.assertEqual(initialized['serverInfo']['version'], version)
        self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n')
        self.process.stdin.flush()

    def tearDown(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.reader.join(timeout=5)
        self.process.stdout.close()
        self.process.stderr.close()
        self.fixture.tearDown()

    def send(self, method, params):
        self.sequence += 1
        self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': self.sequence,
                                            'method': method, 'params': params}) + '\n')
        self.process.stdin.flush()
        return self.sequence

    def receive(self, request_id):
        while request_id not in self.pending:
            response = self.responses.get(timeout=15)
            self.assertIsInstance(response, dict, response)
            if 'id' in response:
                self.pending[response['id']] = response
        return self.pending.pop(request_id)

    def call(self, name, arguments, ok=True):
        response = self.receive(self.send('tools/call', {'name': name, 'arguments': arguments}))
        self.assertNotIn('error', response, response)
        result = response['result']
        self.assertEqual(bool(result.get('isError')), not ok, result)
        return result['structuredContent']

    def test_assignment_protocol_and_retry(self):
        v = self.call('capture_version', {'source_ref': 'source-id', 'source_agent': 'codex'})
        a = self.call('assign_version', {'version_id': v['id'], 'recipient': 'claude-desktop',
                                        'instructions': 'M6-MCP-next-step'})
        self.assertEqual(a['status'], 'pending')
        self.fixture.source.unlink()
        items = self.call('list_assignments', {'recipient': 'claude-desktop'})['assignments']
        self.assertEqual(items[0]['id'], a['id'])
        self.call('claim_assignment', {'assignment_id': a['id'], 'recipient': 'codex'}, ok=False)
        claimed = self.call('claim_assignment', {'assignment_id': a['id'], 'recipient': 'claude-desktop'})
        retry = self.call('claim_assignment', {'assignment_id': a['id'], 'recipient': 'claude-desktop'})
        self.assertEqual(claimed['plan']['target_id'], retry['plan']['target_id'])
        self.assertIn('M6-MCP-next-step', Path(claimed['plan']['target_path']).read_text())

    def test_independent_version_mcp_after_source_removed(self):
        f = self.fixture
        version = self.call('capture_version', {'source_ref': 'source-id', 'source_agent': 'codex'})
        self.assertEqual(version['message_count'], 4)
        f.source_root.rename(f.root / 'unavailable-source')
        f.note.rename(f.root / 'unavailable-note')
        listing = self.call('list_versions', {'task_id': version['task_id']})
        self.assertEqual([v['id'] for v in listing['versions']], [version['id']])
        plan = self.call('prepare_handoff', {'version_id': version['id'], 'target': 'claude-desktop'})
        self.assertEqual(plan['version_id'], version['id'])
        applied = self.call('apply_handoff', {'plan_id': plan['id']})
        text = Path(applied['target_path']).read_text()
        for marker in ('history-only-97c3', 'array-output-A', 'only-in-memory-38a1'):
            self.assertIn(marker, text)
        self.assertEqual(self.call('apply_handoff', {'plan_id': plan['id']})['target_id'], plan['target_id'])
        self.assertIn('array-output-A', self.call('read_context',
            {'handoff_id': version['id'], 'section': 'transcript'})['text'])

    def test_body_search_and_pagination_without_handoff(self):
        f = self.fixture
        first = self.call('find_sessions', {'agent': 'codex', 'limit': 1})
        self.assertGreaterEqual(first['total'], 1)
        item = first['sessions'][0]
        marker = 'm4-only-body-独有词'
        # Append a genuine message that metadata-only matching cannot find.
        message = {'type': 'response_item', 'timestamp': '2026-11-08T00:00:00Z',
                   'payload': {'type': 'message', 'role': 'user',
                               'content': [{'type': 'input_text', 'text': marker}]}}
        with Path(item['source_ref']).open('a') as stream:
            stream.write(json.dumps(message) + '\n')
        found = self.call('find_sessions', {'agent': 'codex', 'query': marker, 'limit': 1})
        self.assertEqual([row['id'] for row in found['sessions']], [item['id']])
        self.assertEqual(found['total'], 1)
        self.assertIsNone(found['next_cursor'])
        end = self.call('find_sessions', {'agent': 'codex', 'query': marker, 'offset': 1})
        self.assertEqual(end['sessions'], [])
        self.assertIsNone(end['next_cursor'])
        import uuid
        second_id = str(uuid.uuid4())
        original = Path(item['source_ref'])
        duplicate = original.with_name('rollout-second-' + second_id + '.jsonl')
        duplicate.write_text(original.read_text().replace(item['id'], second_id))
        page1 = self.call('find_sessions', {'query': marker, 'limit': 1})
        self.assertEqual(page1['total'], 2)
        self.assertEqual(page1['next_cursor'], 1)
        page2 = self.call('find_sessions', {'query': marker, 'limit': 1,
                                           'offset': page1['next_cursor']})
        self.assertEqual({page1['sessions'][0]['id'], page2['sessions'][0]['id']},
                         {item['id'], second_id})
        self.assertIsNone(page2['next_cursor'])
        empty = self.call('find_sessions', {'query': 'm4-no-such-content'})
        self.assertEqual(empty['total'], 0)
        self.assertEqual(empty['sessions'], [])
        import sqlite3
        with sqlite3.connect(f.root / 'state' / 'handoff.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM plans').fetchone()[0], 0)

    def test_tools_and_cli_roundtrip(self):
        names = self.receive(self.send('tools/list', {}))['result']['tools']
        self.assertEqual({x['name'] for x in names}, {
            'find_sessions', 'preview_handoff', 'apply_handoff', 'get_handoff', 'read_context',
            'capture_version', 'list_versions', 'prepare_handoff',
            'assign_version', 'list_assignments', 'claim_assignment', 'list_agents', 'register_agent'})
        sessions = self.call('find_sessions', {'agent': 'codex', 'project': str(self.fixture.work)})
        self.assertEqual(sessions, self.fixture.cli('sessions', '--project', str(self.fixture.work)))
        first = self.call('preview_handoff', {'source_ref': 'source-id', 'target': 'claude-desktop'})
        self.fixture.cli('apply', first['id'])
        target = Path(first['target_path'])
        # Append a real Claude record to show that the return includes new work.
        with target.open('a') as file:
            file.write(json.dumps({'type': 'assistant', 'sessionId': first['target_id'],
                'uuid': 'new-work', 'parentUuid': None, 'timestamp': '2026-11-08T10:02:00Z',
                'cwd': str(self.fixture.work), 'message': {'role': 'assistant',
                'content': [{'type': 'thinking', 'thinking': 'foreign-visible-thinking', 'signature': 'foreign-signature'}, {'type': 'text', 'text': 'claude-new-work-561e'}]}}) + '\n')
        second = self.call('preview_handoff', {'source_ref': str(target), 'source_agent': 'claude', 'target': 'codex'})
        result = self.call('apply_handoff', {'plan_id': second['id']})
        self.assertEqual(result['task_id'], first['task_id'])
        self.assertEqual(result['parent_plan_id'], first['id'])
        self.assertIn('foreign-visible-thinking', self.call('read_context', {'handoff_id': result['id'], 'section': 'transcript'})['text'])
        body = Path(result['target_path']).read_text()
        self.assertNotIn('foreign-signature', body)
        self.assertNotIn('foreign-visible-thinking', body)
        self.assertNotIn('\"type\":\"reasoning\"', body)
        for token in ('history-only-97c3', 'only-in-memory-38a1', 'claude-new-work-561e'):
            self.assertIn(token, body)
        self.assertEqual(self.fixture.cli('status', result['id']), result)
        self.assertEqual(self.call('get_handoff', {'handoff_id': result['id']}), result)

    def test_concurrent_protocol_apply_and_cli_retry(self):
        plan = self.fixture.preview()
        ids = [self.send('tools/call', {'name': 'apply_handoff', 'arguments': {'plan_id': plan['id']}}) for _ in range(2)]
        results = [self.receive(i)['result']['structuredContent'] for i in ids]
        self.assertEqual(results[0]['target_id'], results[1]['target_id'])
        self.assertEqual(len(list((self.fixture.root / 'claude').glob('*/*.jsonl'))), 1)
        self.assertEqual(self.fixture.cli('apply', plan['id'])['target_id'], results[0]['target_id'])

    def test_long_snapshot_read_and_errors(self):
        rows = [json.loads(l) for l in self.fixture.source.read_text().splitlines()]
        text = '长记录🙂' * 8000
        rows[1]['payload']['content'][0]['text'] = text
        self.fixture.source.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        plan = self.fixture.preview()
        context = []
        cursor = 0
        while cursor is not None:
            chunk = self.call('read_context', {'handoff_id': plan['id'], 'section': 'transcript', 'cursor': cursor})
            self.assertLessEqual(len(chunk['text']), 16000)
            context.append(chunk['text'])
            cursor = chunk['next_cursor']
        self.assertIn(text, ''.join(context))
        self.fixture.source.write_text('source changed after snapshot')
        self.call('apply_handoff', {'plan_id': plan['id']}, ok=False)
        self.call('read_context', {'handoff_id': plan['id'], 'section': '../outside-section'}, ok=False)
        self.call('get_handoff', {'handoff_id': 'missing'}, ok=False)
        self.assertEqual(self.call('get_handoff', {'handoff_id': plan['id']})['status'], 'previewed')

    def test_m1_persisted_plan_is_compatible(self):
        import sqlite3
        plan = self.fixture.preview()
        with sqlite3.connect(self.fixture.root / 'state/handoff.sqlite3') as db:
            old, = db.execute('SELECT body FROM plans WHERE id=?', [plan['id']]).fetchone()
            body = json.loads(old)
            for field in ('source_agent', 'target_agent', 'parent_plan_id', 'version_id', 'verify_source'):
                body.pop(field, None)
            db.execute('UPDATE plans SET body=? WHERE id=?', [json.dumps(body), plan['id']])
        result = self.call('apply_handoff', {'plan_id': plan['id']})
        self.assertEqual(result['target_agent'], 'claude-desktop')
        self.assertIsNone(result['parent_plan_id'])


if __name__ == '__main__':
    unittest.main()
