"""Create isolated, distinguishable Git worktrees and a synthetic chat for M0."""
import json
import secrets
import subprocess
import uuid
from pathlib import Path


def git(directory, *args):
    subprocess.run(["git", "-C", directory, *args], check=True)


def create(root):
    root.mkdir(parents=True)
    control = root / "control"
    target = root / "target"
    control.mkdir()
    git(control, "init", "-q", "-b", "control")
    (control / "marker.txt").write_text("control-" + secrets.token_hex(5) + "\n")
    git(control, "add", "marker.txt")
    git(control, "-c", "user.name=M0", "-c", "user.email=m0@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "建立接手验证对照")
    git(control, "worktree", "add", "-q", "-b", "target", str(target))
    (target / "marker.txt").write_text("target-" + secrets.token_hex(5) + "\n")
    git(target, "add", "marker.txt")
    (target / "untracked.txt").write_text("untracked-" + secrets.token_hex(5) + "\n")
    session = {
        "id": str(uuid.uuid4()), "cwd": str(target), "title": "Handoff M0",
        "messages": [
            {"role": "user", "content": "本次 M0 历史识别词为 " + secrets.token_hex(6)},
            {"role": "assistant", "content": "已记录，可继续验证工作目录。"},
        ],
    }
    (root / "source.json").write_text(json.dumps(session, ensure_ascii=False))
    print(root)


if __name__ == "__main__":
    create(Path(__file__).resolve().parents[1] / ".m0" / uuid.uuid4().hex[:12])
