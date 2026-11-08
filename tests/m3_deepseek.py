"""Verify current Harness SDK persistence and all new conversion directions in isolated roots."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
import uuid
import m1_cli
import m2_mcp


class DeepSeekTests(unittest.TestCase):
    def setUp(self):
        self.fixture = m1_cli.CliTests()
        self.fixture.setUp()
        executable = shutil.which('dsh')
        self.assertIsNotNone(executable, 'Install DeepSeek Harness to run this SDK integration suite')
        self.package = Path(executable).resolve().parent.parent
        self.root = self.fixture.root / 'dsh'

    def tearDown(self):
        self.fixture.tearDown()

    def cli(self, *args, **kwargs):
        return self.fixture.cli('--dsh-root', str(self.root), '--dsh-package', str(self.package), *args, **kwargs)

    def continue_native(self, native_id):
        # Append through the real persistence API as the running Harness does.
        script = r'''
const {createRequire}=await import('node:module');
const require=createRequire(process.argv[1]+'/package.json');
const load=async n=>import(require.resolve(n));
const {Context}=await load('@deepseek-ai/cordis');
const backend=await load('@deepseek-ai/dsh-session-persistence-jsonl');
const {Session}=await load('@deepseek-ai/dsh-session');
const {createSystemMessage,createUserMessage,createAssistantMessage}=await load('@deepseek-ai/dsh-llm');
const ctx=new Context();const scope=await ctx.plugin(backend,{root:process.argv[2]});
const h=await ctx.sessionPersistence.open(process.argv[3],'write');
try {
 const {events,eventState}=await h.read();const session=Session.fromRestore(h.header.id,events,h.header,h.inheritedEventCount,eventState);
 const turn=Math.max(0,...events.filter(e=>e.type==='turn/start').map(e=>e.data.turn))+1;
 const added=[];for(let seq=events.length;seq<session.seq;seq++)added.push(session.eventAt(seq));
 const append=(t,d,o)=>added.push(session.append(t,d,o));
 append('turn/start',{turn});append('step/start',{turn,step:1});
 append('system/message',{turn,step:1,message:createSystemMessage('runtime system')},{surfaceOp:'append'});
 append('user/message',createUserMessage({content:[{type:'text',text:'continue-native'}],source:{kind:'user'}}),{surfaceOp:'append'});
 append('assistant/message',{turn,step:1,stream:[],message:createAssistantMessage({content:[{type:'text',text:'deepseek-new-work-a82c'}],source:{provider:'test',model:'test'}})},{surfaceOp:'append'});
 append('step/end',{turn,step:1});append('turn/end',{turn,reason:{kind:'completed'}});
 await h.append(added);await h.flush();
}finally{await h.close();await scope.dispose();}
'''
        result = subprocess.run(['node', '--input-type=module', '-e', script, str(self.package), str(self.root), native_id], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_compressed_native_continuation_and_exports(self):
        plan = self.cli('preview', 'source-id', '--to', 'deepseek')
        self.assertIn('DeepSeek Harness', plan['next_action'])
        result = self.cli('apply', plan['id'], '--open')
        self.assertEqual(result['status'], 'waiting_for_open')
        self.assertIn('现有 Harness', result['open_error'])
        target = Path(result['target_path'])
        self.assertTrue(target.name.endswith('.zstd'))
        self.continue_native(plan['target_id'])
        before = target.read_bytes()
        listed = self.cli('sessions', '--agent', 'deepseek')['sessions']
        self.assertEqual(listed[0]['id'], plan['target_id'])
        for destination in ['codex', 'claude-desktop']:
            exported = self.cli('preview', plan['target_id'], '--from', 'deepseek', '--to', destination)
            self.cli('apply', exported['id'])
            body = Path(exported['target_path']).read_text()
            for word in ['deepseek-new-work-a82c', 'history-only-97c3', 'only-in-memory-38a1', 'array-output-A', 'array-output-B']:
                self.assertIn(word, body)
            self.assertEqual(exported['parent_plan_id'], plan['id'])
            self.assertEqual(exported['task_id'], plan['task_id'])
            self.assertEqual(exported['workspace']['cwd'], str(self.fixture.work))
        self.assertEqual(target.read_bytes(), before)
        self.cli('apply', plan['id'])
        self.assertEqual(target.read_bytes(), before)

    def test_claude_to_deepseek_and_existing_root(self):
        first = self.fixture.preview()
        self.fixture.cli('apply', first['id'])
        second = self.cli('preview', first['target_id'], '--from', 'claude', '--to', 'deepseek')
        self.cli('apply', second['id'])
        self.continue_native(second['target_id'])
        third = self.cli('preview', second['target_id'], '--from', 'deepseek', '--to', 'codex')
        self.assertEqual(third['task_id'], first['task_id'])
        self.assertEqual(third['parent_plan_id'], second['id'])

    def test_claude_parallel_calls_across_assistant_records(self):
        first = self.fixture.preview()
        self.fixture.cli('apply', first['id'])
        source = Path(first['target_path'])
        rows = [json.loads(line) for line in source.read_text().splitlines()]
        assistant = next(r for r in rows if r.get('type') == 'assistant')
        user = next(r for r in rows if r.get('type') == 'user')
        previous = rows[-1].get('uuid')
        for role, content in [
            ('assistant', [{'type': 'tool_use', 'id': 'parallel-a', 'name': 'Edit', 'input': {'file': 'a'}}]),
            ('assistant', [{'type': 'tool_use', 'id': 'parallel-b', 'name': 'Edit', 'input': {'file': 'b'}}]),
            ('user', [{'type': 'tool_result', 'tool_use_id': 'parallel-a', 'content': 'parallel-result-a'}]),
            ('user', [{'type': 'tool_result', 'tool_use_id': 'parallel-b', 'content': 'parallel-result-b'}]),
            ('assistant', [{'type': 'text', 'text': 'both edits completed'}]),
        ]:
            row = json.loads(json.dumps(assistant if role == 'assistant' else user))
            row['uuid'] = str(uuid.uuid4())
            row['parentUuid'] = previous
            row['message']['content'] = content
            previous = row['uuid']
            rows.append(row)
        source.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        plan = self.cli('preview', first['target_id'], '--from', 'claude', '--to', 'deepseek')
        self.cli('apply', plan['id'])
        self.continue_native(plan['target_id'])
        exported = self.cli('preview', plan['target_id'], '--from', 'deepseek', '--to', 'codex')
        self.assertFalse(Path(exported['target_path']).exists())
        result = self.cli('apply', exported['id'])
        body = Path(result['target_path']).read_text()
        for text in ['parallel-result-a', 'parallel-result-b', 'both edits completed']:
            self.assertIn(text, body)

    def test_unavailable_sdk_does_not_publish(self):
        error = self.fixture.cli('--dsh-root', str(self.root), '--dsh-package', str(self.fixture.root / 'absent'), 'preview', 'source-id', '--to', 'deepseek', ok=False)
        self.assertTrue(error)
        self.assertFalse(self.root.exists())

    def test_mcp_deepseek_source_and_cli_status(self):
        plan = self.cli('preview', 'source-id', '--to', 'deepseek')
        self.cli('apply', plan['id'])
        client = m2_mcp.McpTests()
        client.extra_args = ['--dsh-root', str(self.root), '--dsh-package', str(self.package)]
        client.setUp()
        try:
            sessions = client.call('find_sessions', {'agent': 'deepseek'})
            self.assertEqual(sessions['sessions'][0]['id'], plan['target_id'])
            exported = client.call('preview_handoff', {'source_ref': plan['target_id'],
                                   'source_agent': 'deepseek', 'target': 'codex'})
            result = client.call('apply_handoff', {'plan_id': exported['id']})
            self.assertEqual(result['target_id'], exported['target_id'])
            self.assertEqual(client.fixture.cli('status', exported['id']),
                             client.call('get_handoff', {'handoff_id': exported['id']}))
            self.assertIn('history-only-97c3', Path(result['target_path']).read_text())
        finally:
            client.tearDown()


if __name__ == '__main__':
    unittest.main()
