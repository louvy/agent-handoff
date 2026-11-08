"""跨客户端组合验收探针。使用临时 HOME、真实 Git worktree 与本机 Harness SDK。"""
import itertools
import json
import os
from pathlib import Path
import sqlite3
import time
from unittest.mock import patch

import m2_mcp


TARGET = {'codex': 'codex', 'claude': 'claude-desktop', 'deepseek': 'deepseek'}


def run(server):
    f = server.fixture
    call = server.call
    cli = f.cli
    ids = {'codex': 'source-id'}
    for agent in ('claude', 'deepseek'):
        plan = cli('preview', 'source-id', '--to', TARGET[agent])
        ids[agent] = cli('apply', plan['id'])['target_id']

    observations = []

    expected_errors = {
        'missing-session': 'SESSION_NOT_FOUND', 'ambiguous-id': 'SESSION_AMBIGUOUS',
        'malformed-session': 'SOURCE_INVALID', 'no-cwd': 'WORKSPACE_UNSPECIFIED',
        'missing-worktree': 'WORKSPACE_MISSING', 'non-git': 'WORKSPACE_INVALID',
        'missing-assignment': 'ASSIGNMENT_NOT_FOUND', 'missing-version': 'VERSION_NOT_FOUND',
        'wrong-recipient': 'RECIPIENT_MISMATCH', 'claim-worktree-gone': 'WORKSPACE_MISSING',
        'publish-failure': 'PATH_CONFLICT', 'publish-denied': 'ACCESS_DENIED',
        'published-worktree-gone': 'WORKSPACE_MISSING', 'target-lost': 'TARGET_MISSING',
        'broken-sdk': 'ADAPTER_UNAVAILABLE', 'launcher-missing': 'CLIENT_UNAVAILABLE',
        'head-drift': 'WORKSPACE_CHANGED', 'state-busy': 'STATE_BUSY',
        'state-corrupt': 'STATE_CORRUPT', 'invalid-mode': 'INVALID_REQUEST',
    }

    def record(case, result, **details):
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                pass
        prefix = case.split('/')[0]
        if prefix in expected_errors:
            assert result['code'] == expected_errors[prefix], (case, result)
            assert result['error'] and result['next_action']
            assert isinstance(result['retryable'], bool)
            assert not any(token in json.dumps(result) for token in (
                'os error', 'Query returned', 'node:internal', 'EOF while', 'Error code'))
        observations.append(dict(case=case, result=result, **details))

    for src, dst in itertools.permutations(TARGET, 2):
        ref, recipient = ids[src], TARGET[dst]
        listing = call('find_sessions', {'agent': src, 'query': ref, 'project': str(f.work)})
        entry = next(row for row in listing['sessions'] if row['id'] == ref)
        source_path = Path(entry['source_ref'])
        original = source_path.read_bytes()
        for mode in ('pull', 'push'):
            markers = ['history-only-97c3', 'only-in-memory-38a1', 'array-output-A', 'array-output-B']
            if mode == 'pull':
                preview = call('preview_handoff', {'source_agent': src, 'source_ref': str(source_path),
                                                   'target': recipient})
                assert not Path(preview['target_path']).exists()
                plan = call('apply_handoff', {'plan_id': preview['id']})
            else:
                version = cli('capture', ref, '--from', src)
                instructions = f'continue-{src}-{dst}'
                markers.append(instructions)
                assigned = cli('assign', version['id'], '--to', recipient, '--instructions', instructions)
                assert assigned['status'] == 'pending' and assigned['plan'] is None
                claims = {'recipient': recipient, 'assignment_id': assigned['id']}
                inbox = call('list_assignments', {'recipient': recipient})
                assert any(a['id'] == assigned['id'] for a in inbox['assignments'])
                plan = call('claim_assignment', claims)['plan']
                assert call('claim_assignment', claims)['plan']['target_id'] == plan['target_id']
            native = cli('capture', plan['target_id'], '--from', dst)
            transcript = cli('read', native['id'], '--section', 'transcript')['text']
            assert all(word in transcript for word in markers), (src, dst, mode)
            assert native['parent_plan_id'] == plan['id']
            assert native['workspace']['root'] == native['workspace']['cwd'] == str(f.work)
            assert source_path.read_bytes() == original
            assert f.git(f.work, 'status', '--porcelain') == b'M  marker.txt\n'
            record(f'{src}->{dst}/{mode}', 'pass')

    def pull(src, ok=False):
        dst = 'codex' if src != 'codex' else 'claude-desktop'
        result = call('preview_handoff', {'source_agent': src, 'source_ref': ids[src], 'target': dst}, ok=ok)
        if not ok:
            assert json.loads(cli('preview', ids[src], '--from', src, '--to', dst, ok=False)) == result
        return result

    for src in TARGET:
        record(f'missing-session/{src}', call('preview_handoff', {
            'source_agent': src, 'source_ref': 'no-such-session', 'target': 'codex'}, ok=False))
    cli_error = json.loads(cli('preview', 'source-id', '--mode', 'invalid', ok=False))
    mcp_error = call('preview_handoff', {
        'source_agent': 'codex', 'source_ref': 'source-id', 'target': 'codex', 'mode': 'invalid'}, ok=False)
    assert cli_error == mcp_error
    record('invalid-mode', cli_error)
    original = f.source.read_bytes()
    duplicate = f.source.with_name('rollout-duplicate.jsonl')
    duplicate.write_bytes(original)
    record('ambiguous-id', pull('codex'))
    duplicate.unlink()
    good_plan = cli('preview', ids['claude'], '--from', 'claude', '--to', 'codex')
    good_id = cli('apply', good_plan['id'])['target_id']
    with f.source.open('ab') as stream:
        stream.write(b'{\n')
    record('malformed-session', pull('codex'))
    good = cli('capture', good_id)
    assert good['source_id'] == good_id
    response = server.receive(server.send('tools/call', {'name': 'find_sessions', 'arguments': {
        'agent': 'codex', 'query': good_id}}))['result']
    result = response['structuredContent']
    assert not response.get('isError'), result
    assert result['partial'] and result['warnings'], result
    assert any(r['id'] == good_id for r in result['sessions']), result
    record('find-healthy-with-broken-neighbor', 'pass', warnings=result['warnings'])
    f.source.write_bytes(original)
    rows = [json.loads(line) for line in original.splitlines()]
    del rows[0]['payload']['cwd']
    f.source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    record('no-cwd', pull('codex'))
    f.write_source(f.control)
    record('ordinary-git-control', pull('codex', ok=True)['workspace']['root'] == str(f.control))
    f.source.write_bytes(original)
    moved = f.root / 'parked'
    f.work.rename(moved)
    for src in TARGET:
        record(f'missing-worktree/{src}', pull(src))
    f.work.mkdir()
    for src in TARGET:
        record(f'non-git/{src}', pull(src))
    f.work.rmdir()
    moved.rename(f.work)
    record('missing-assignment', call('claim_assignment', {
        'recipient': 'codex', 'assignment_id': 'absent'}, ok=False))
    result = json.loads(cli('prepare', 'absent', '--to', 'codex', ok=False))
    assert result == call('prepare_handoff', {'version_id': 'absent', 'target': 'codex'}, ok=False)
    record('missing-version', result)
    for src in TARGET:
        cli_error = json.loads(cli('preview', 'no-such-session', '--from', src, '--to', 'codex', ok=False))
        mcp_error = call('preview_handoff', {
            'source_agent': src, 'source_ref': 'no-such-session', 'target': 'codex'}, ok=False)
        assert cli_error == mcp_error
        record(f'cli-mcp-parity/{src}', 'pass')
    version = cli('capture', ids['codex'])
    for agent, recipient in TARGET.items():
        a = cli('assign', version['id'], '--to', recipient, '--instructions', 'retry acceptance')
        args = ['claim', a['id'], '--recipient', recipient]
        other = 'claude-desktop' if agent == 'codex' else 'codex'
        record(f'wrong-recipient/{agent}', cli('claim', a['id'], '--recipient', other, ok=False))
        f.work.rename(moved)
        cli_error = json.loads(cli(*args, ok=False))
        mcp_error = call('claim_assignment', {'assignment_id': a['id'], 'recipient': recipient}, ok=False)
        assert cli_error == mcp_error
        record(f'claim-worktree-gone/{agent}', cli_error)
        preview = cli('inbox', '--recipient', recipient)['assignments'][0]['plan']
        moved.rename(f.work)
        path = Path(preview['target_path'])
        path.mkdir(parents=True)
        result = json.loads(cli(*args, ok=False))
        assert result == call('claim_assignment', {'assignment_id': a['id'], 'recipient': recipient}, ok=False)
        record(f'publish-failure/{agent}', result)
        path.rmdir()
        # The target parent is a fixture directory. Restore access even on failure.
        perms = path.parent.stat().st_mode & 0o777
        if os.geteuid() != 0:
            path.parent.chmod(0o500)
            try:
                record(f'publish-denied/{agent}', cli(*args, ok=False))
            finally:
                path.parent.chmod(perms)
        recovered = cli(*args)
        assert recovered['plan']['target_id'] == preview['target_id'] and path.is_file()
        record(f'publish-retry/{agent}', recovered['status'])
        f.work.rename(moved)
        try:
            retry = cli(*args, ok=False)
            record(f'published-worktree-gone/{agent}', retry, cwd_exists=f.work.exists())
        finally:
            moved.rename(f.work)
        path.unlink()
        again = cli(*args, ok=False)
        assert json.loads(again) == call('claim_assignment', {
            'assignment_id': a['id'], 'recipient': recipient}, ok=False)
        record(f'target-lost/{agent}', again, exists=path.exists())
        assert not path.exists()
    bad = f.root / 'empty-package'
    bad.mkdir()
    assignment = cli('assign', version['id'], '--to', 'deepseek', '--instructions', 'test sdk')
    def snapshots():
        return set((f.root / 'state').glob('*/transcript.txt'))

    before = snapshots()
    for _ in range(2):
        message = cli('--dsh-package', str(bad), 'claim', assignment['id'], '--recipient', 'deepseek', ok=False)
    pending = cli('inbox', '--recipient', 'deepseek')['assignments'][0]
    assert not snapshots() - before, 'SDK failure leaked transcripts'
    record('broken-sdk/two-attempts', message, stack='at ' in message,
           status=pending['status'], plan=pending['plan'], orphan_transcripts=len(snapshots() - before))
    recovered = cli('claim', assignment['id'], '--recipient', 'deepseek')
    record('sdk-restored', recovered['status'])
    plan = cli('preview', 'source-id')
    with patch.dict(os.environ, {'HANDOFF_CLAUDE_BIN': str(f.root / 'missing-launcher')}):
        opened = cli('apply', plan['id'], '--open')
    record('launcher-missing', opened['open_issue'], status=opened['status'],
           target_exists=Path(opened['target_path']).is_file())
    fake_bin = f.root / 'fake-bin'
    fake_bin.mkdir()
    opener = fake_bin / 'open'
    opener.write_text('#!/bin/sh\nexit 1\n')
    opener.chmod(0o700)
    with patch.dict(os.environ, {'PATH': str(fake_bin) + os.pathsep + os.environ['PATH']}):
        for agent in ('codex', 'deepseek'):
            plan = cli('preview', 'source-id', '--to', agent)
            opened = cli('apply', plan['id'], '--open')
            assert opened['open_issue']['code'] == ('CLIENT_OPEN_FAILED' if agent == 'codex' else 'MANUAL_OPEN_REQUIRED')
            record(f'open/{agent}', opened['open_issue'], status=opened['status'],
                   target_exists=Path(opened['target_path']).is_file())
    plan = cli('preview', 'source-id')
    f.git(f.work, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
          '-c', 'commit.gpgsign=false', 'commit', '-qm', '验收 HEAD 变动')
    record('head-drift', cli('apply', plan['id'], ok=False),
           target_exists=Path(plan['target_path']).exists())
    assert cli('apply', recovered['plan']['id'])['target_id'] == recovered['plan']['target_id']
    record('published-head-advance-control', 'pass')
    sdk_server = m2_mcp.McpTests()
    sdk_server.extra_args = ['--dsh-package', str(bad)]
    sdk_server.setUp()
    try:
        sdk_version = sdk_server.fixture.cli('capture', 'source-id')
        cli_error = json.loads(sdk_server.fixture.cli('--dsh-package', str(bad),
            'prepare', sdk_version['id'], '--to', 'deepseek', ok=False))
        mcp_error = sdk_server.call('prepare_handoff', {
            'version_id': sdk_version['id'], 'target': 'deepseek'}, ok=False)
        assert cli_error == mcp_error and cli_error['code'] == 'ADAPTER_UNAVAILABLE'
        assert not list((sdk_server.fixture.root / 'state').glob('*/transcript.txt'))
        record('sdk-cli-mcp-parity', 'pass')
    finally:
        sdk_server.tearDown()
    with sqlite3.connect(f.root / 'state/handoff.sqlite3') as db:
        db.execute('BEGIN IMMEDIATE')
        started = time.monotonic()
        cli_error = json.loads(cli('capture', ids['codex'], ok=False))
        cli_seconds = time.monotonic() - started
        started = time.monotonic()
        # SQLite deliberately waits up to 10 seconds per lock acquisition.
        # Leave room for lock retries and runner scheduling in this negative case.
        assert cli_error == call('capture_version', {
            'source_agent': 'codex', 'source_ref': ids['codex']}, ok=False, timeout=30)
        record('state-busy', cli_error, cli_seconds=round(cli_seconds, 2),
               mcp_seconds=round(time.monotonic() - started, 2))
    recovered = call('capture_version', {
        'source_agent': 'codex', 'source_ref': ids['codex']})
    assert recovered['source_id'] == ids['codex']
    record('state-busy-recovered', 'pass')
    state = f.root / 'state/handoff.sqlite3'
    state.write_bytes(b'invalid test database')
    result = call('list_versions', {}, ok=False)
    assert result == json.loads(cli('versions', ok=False))
    record('state-corrupt', result)
    return observations


if __name__ == '__main__':
    # Force both independent processes to use the same isolated default SDK root.
    with patch.dict(os.environ):
        os.environ.pop('DSH_HOME', None)
        server = m2_mcp.McpTests()
        server.setUp()
        try:
            print(json.dumps(run(server), ensure_ascii=False, indent=2))
        finally:
            server.tearDown()
