// DeepSeek Harness 0.2.2-rc.1 / session format 4 bridge.
// Resolve the installed Harness SDK, without mounting its model or credential plugins.
import { createRequire } from 'node:module';
let inputText = '';
for await (const chunk of process.stdin) inputText += chunk;
const request = JSON.parse(inputText);
const require = createRequire(process.argv[1] + '/package.json');
const load = async (name) => import(require.resolve(name));
const { Context } = await load('@deepseek-ai/cordis');
const persistence = await load('@deepseek-ai/dsh-session-persistence-jsonl');
const { Session, SessionId, SESSION_FORMAT_VERSION } = await load('@deepseek-ai/dsh-session');
const { createUserMessage, createAssistantMessage, createToolResultMessage, createSystemMessage, ToolCallId } = await load('@deepseek-ai/dsh-llm');
if (SESSION_FORMAT_VERSION !== 4) throw new Error('Unsupported DeepSeek session format');
const ctx = new Context();
const scope = await ctx.plugin(persistence, { root: request.root });
const store = ctx.sessionPersistence;
const textOf = (blocks) => blocks.map(b => {
    if (b.type !== 'text') throw new Error('Non-text tool result requires a media adapter');
    return b.text;
}).join('\n');
function toSimple(message) {
    if (message.role === 'system') return { role: 'user', content: message.content };
    if (message.role === 'tool') return { role: 'user', content: [{
        type: 'tool_result', tool_use_id: message.toolCallId,
        content: textOf(message.content), is_error: message.isError,
    }] };
    return { role: message.role === 'assistant' ? 'assistant' : 'user', content: message.content.map(b => {
        if (b.type === 'text') return b;
        if (b.type === 'reasoning') return { type: 'thinking', text: b.text };
        if (b.type === 'tool-call') return { type: 'tool_use', id: b.id, name: b.name, input: JSON.parse(b.arguments) };
        throw new Error(`Unsupported DeepSeek content block ${b.type}`);
    }) };
}
async function run() {
    if (request.op === 'discover') {
        return (await store.list()).map(s => ({ id: s.header.id, cwd: s.header.cwd,
            timestamp: new Date(s.header.createdAt).toISOString(), reference: s.location.path }));
    }
    if (request.op === 'validate') {
        try {
            const handle = await store.open(SessionId(request.id), 'read');
            try {
                const { events, eventState } = await handle.read();
                const session = Session.fromRestore(handle.header.id, events, handle.header, handle.inheritedEventCount, eventState);
                const stat = await store.stat(SessionId(request.id));
                return { valid: handle.header.id === request.id && handle.header.cwd === request.cwd
                    && stat.location.path === request.path && session.deriveMessages().length > 0 };
            } finally { await handle.close(); }
        } catch (error) {
            return { valid: false, issue: ['EACCES', 'EPERM'].includes(error.code) ? 'ACCESS_DENIED'
                : error.code === 'ENOENT' ? 'TARGET_MISSING' : 'TARGET_INVALID' };
        }
    }
    if (request.op === 'read') {
        const handle = await store.open(SessionId(request.id), 'read');
        try {
            const { events, eventState } = await handle.read();
            const session = Session.fromRestore(handle.header.id, events, handle.header, handle.inheritedEventCount, eventState);
            return { id: session.id, cwd: handle.header.cwd,
                timestamp: new Date(handle.header.createdAt).toISOString(),
                messages: session.deriveMessages().map(toSimple) };
        } finally { await handle.close(); }
    }
    if (request.op !== 'prepare') throw new Error('Unknown DeepSeek operation');
    const input = request.transcript;
    const header = { version: SESSION_FORMAT_VERSION, id: SessionId(input.id),
        createdAt: Date.now(), cwd: input.cwd, isSeeded: false, delegationDepth: 0, agentPreset: 'standard' };
    const session = Session.create(header.id, [], header);
    const events = [];
    for (let seq = 0; seq < session.seq; seq++) events.push(session.eventAt(seq));
    const append = (type, data, options) => {
        const event = session.append(type, data, options);
        events.push(event);
        return event;
    };
    append('turn/start', { turn: 1 });
    append('step/start', { turn: 1, step: 1 });
    append('system/message', { turn: 1, step: 1, message: createSystemMessage('') }, { surfaceOp: 'append' });
    let turn = 1, step = 1, inTurn = true, inStep = true;
    const closeStep = () => {
        if (inStep) append('step/end', { turn, step });
        inStep = false;
    };
    const closeTurn = () => {
        closeStep();
        if (inTurn) append('turn/end', { turn, reason: { kind: 'completed' } });
        inTurn = false;
    };
    const openTurn = () => {
        if (!inTurn) { turn++; step = 0; append('turn/start', { turn }); inTurn = true; }
    };
    const openStep = () => {
        openTurn();
        if (!inStep) { step++; append('step/start', { turn, step }); inStep = true; }
    };
    const calls = new Map();
    for (const item of input.messages) {
        const blocks = typeof item.content === 'string' ? [{ type: 'text', text: item.content }] : item.content;
        const results = blocks.filter(b => b.type === 'tool_result');
        const content = blocks.filter(b => b.type !== 'tool_result').map(b => {
            if (b.type === 'text') return b;
            if (b.type === 'tool_use') return { type: 'tool-call', id: ToolCallId(b.id), name: b.name, arguments: JSON.stringify(b.input) };
            throw new Error(`Unsupported imported block ${b.type}`);
        });
        if (item.role === 'assistant' && content.length) {
            if (calls.size === 0) closeStep();
            openStep();
            append('assistant/message', { turn, step, stream: [], message: createAssistantMessage({
                content, source: { provider: 'handoff', model: 'imported' },
            }) }, { surfaceOp: 'append' });
            for (const b of content.filter(b => b.type === 'tool-call')) {
                const call = append('tool/call', { turn, step, callId: b.id, name: b.name, arguments: b.arguments });
                calls.set(b.id, call.seq);
            }
        } else if (content.length) {
            closeTurn(); openTurn();
            append('user/message', createUserMessage({ content, source: { kind: 'user' } }), { surfaceOp: 'append' });
        }
        for (const b of results) {
            openStep();
            if (!calls.has(b.tool_use_id)) throw new Error('Unpaired imported tool result');
            append('tool/result', { turn, step, message: createToolResultMessage({
                callId: ToolCallId(b.tool_use_id), isError: !!b.is_error,
                content: [{ type: 'text', text: typeof b.content === 'string' ? b.content : JSON.stringify(b.content) }],
            }) }, { surfaceOp: 'append', sourceEventSeqs: [calls.get(b.tool_use_id)] });
            calls.delete(b.tool_use_id);
        }
    }
    closeTurn();
    append('session/title', { title: input.title, source: { kind: 'user' }, messageSeqs: [] });
    const handle = await store.create(header);
    try { await handle.append(events); await handle.flush(); }
    finally { await handle.close(); }
    const check = await store.open(header.id, 'read');
    let delivered;
    try {
        const replay = await check.read();
        delivered = Session.fromRestore(header.id, replay.events, check.header, check.inheritedEventCount, replay.eventState)
            .deriveMessages().map(toSimple);
    }
    finally { await check.close(); }
    const stat = await store.stat(header.id);
    return { path: stat.location.path, id: header.id, cwd: header.cwd,
        timestamp: new Date(header.createdAt).toISOString(), messages: delivered };
}
try { process.stdout.write(JSON.stringify(await run()) + '\n'); }
finally { await scope.dispose(); }
