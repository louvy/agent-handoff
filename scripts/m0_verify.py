"""Verify the M0 runtime evidence in one native transcript against a fixture."""
import argparse
import json
import subprocess
from pathlib import Path


def records(path):
    if path.suffix in (".zstd", ".zst"):
        data = subprocess.check_output(["zstd", "-dc", path])
    else:
        data = path.read_bytes()
    return [json.loads(line) for line in data.splitlines() if line.strip()]


def evidence(client, row):
    kind = row.get("type")
    if client == "codex":
        event = row.get("payload", {})
        if kind == "response_item" and event.get("type") in (
            "function_call_output", "custom_tool_call_output"
        ):
            yield "tool", json.dumps(event.get("output"), ensure_ascii=False)
        if kind == "event_msg" and event.get("type") == "agent_message":
            yield "reply", event.get("message", "")
    elif client == "claude":
        blocks = row.get("message", {}).get("content", [])
        if isinstance(blocks, list):
            for block in blocks:
                if kind == "user" and block.get("type") == "tool_result" and not block.get("is_error"):
                    yield "tool", json.dumps(block["content"], ensure_ascii=False)
                if kind == "assistant" and block.get("type") == "text":
                    yield "reply", block["text"]
    else:
        message = row.get("data", {}).get("message", {})
        visible = "\n".join(b["text"] for b in message.get("content", []) if b["type"] == "text")
        if kind == "tool/result" and not message.get("isError"):
            yield "tool", visible
        if kind == "assistant/message":
            yield "reply", visible


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client", choices=["codex", "claude", "deepseek"])
    parser.add_argument("fixture", type=Path)
    parser.add_argument("transcript", type=Path)
    parser.add_argument("--history", help="override the history marker for a native control session")
    args = parser.parse_args()
    source = json.loads((args.fixture / "source.json").read_text())
    target = args.fixture.resolve() / "target"
    history = args.history or source["messages"][0]["content"].split()[-1]
    markers = [str(target), (target / "marker.txt").read_text().strip(),
               (target / "untracked.txt").read_text().strip()]
    control = (args.fixture / "control/marker.txt").read_text().strip()
    saw_read = False
    for row in records(args.transcript):
        for role, body in evidence(args.client, row):
            if all(value in body for value in markers) and control not in body:
                if role == "tool":
                    saw_read = True
                elif saw_read and history in body:
                    print(f"PASS {args.client} {args.transcript.name} {history}")
                    return
    raise SystemExit("FAIL: missing matching native tool output followed by a reply with the history marker")


if __name__ == "__main__":
    main()
