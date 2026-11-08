# 本机 Agent 接入协议 v1

接入方可以是任意语言编写的客户端或桥接程序。各方在同一用户下使用同一个 Handoff 状态目录，并有权访问原 Git worktree。CLI 和 stdio MCP 共用注册表。两端只需实现各自声明的方向。

## 1. 注册与发现

```sh
handoff agents
handoff register-agent --id example-agent --role both --protocol-version 1
```

MCP 对应 `list_agents({})` 和 `register_agent({"id":"example-agent","role":"both","protocol_version":1})`。服务返回绝对目录 `sessions_dir`，以及调用其他接口时使用的 `source_agent`、`target_agent`。不支持的方向返回 `null`。发现接口同时返回支持的版本、文件命名约定和最小会话样例。已经运行的 MCP 实例在下一次调用时就能看到通过 CLI 或另一实例完成的注册。

| 注册字段 | v1 约定 |
|---|---|
| `id` | 匹配 `^[a-z][a-z0-9-]{0,63}$`。内置标识 `codex`、`claude`、`claude-desktop`、`deepseek` 已保留。 |
| `role` | `source` 提供会话，`target` 接收会话，`both` 可接收并将续写交给下一端。 |
| `protocol_version` | 整数 `1`。相同声明可重复注册并获得同一目录。不同能力共用同一 ID 返回 `REGISTRATION_CONFLICT`。 |

注册持久化在本地 SQLite 中。注册标识用于路由，同一用户下的客户端共享信任边界。每个客户端负责自己的目录。注册无需提交代码、修改配置或重启中转站。

## 2. 提供会话

把一个完整的 UTF-8 JSON 对象保存到 `sessions_dir/<meta.id>.json`。文件名使用单个路径组件，严格匹配会话 ID。用同目录临时文件写完后原子替换，临时文件使用其他扩展名。目录、会话文件均须为真实路径。以下 Python 示例在原 Git worktree 内运行，使用注册返回的目录。

```python
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

registration = json.loads(subprocess.check_output([
    "handoff", "register-agent", "--id", "example-agent",
    "--role", "both", "--protocol-version", "1",
]))
now = datetime.now(timezone.utc).isoformat()
session = {
    "protocol_version": 1,
    "meta": {"id": "issue-42", "timestamp": now, "cwd": str(Path.cwd())},
    "messages": [{"role": "user", "timestamp": now,
                  "content": [{"type": "text", "text": "继续修复失败的测试"}]}],
}
directory = Path(registration["sessions_dir"])
tmp = directory / "issue-42.tmp"
tmp.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
tmp.replace(directory / "issue-42.json")
```

`meta.timestamp` 与每条消息的 `timestamp` 均为 RFC 3339 时间。接手需要现存 Git worktree 下的绝对 `cwd`，包括原会话使用的子目录。`meta.title` 可选。消息按时间顺序保存，至少有一条。角色为 `user` 或 `assistant`。`content` 为有序块数组。文本块如上，历史工具调用与结果可按下面的方式配对。完整的块、工具和可选元数据字段使用 txcript 0.14.4 的 `common::{Meta, Message, Block}` 序列化格式。接收端应在续写时完整保留已有消息和结构化块，仅在末尾追加。重复 apply 或 claim 会与交付时的消息前缀比较；删减、改写或丢失笔记及指派要求返回 TARGET_INVALID。允许修改标题和 JSON 排版。工具调用仅供回顾。

```json
[
  {"role":"assistant","timestamp":"2026-01-01T00:00:00Z","content":[
    {"type":"tool_use","id":"t1","tool":{"name":"Raw","tool_name":"check","input":{"suite":"unit"}}}]},
  {"role":"user","timestamp":"2026-01-01T00:00:01Z","content":[
    {"type":"tool_result","tool_use_id":"t1","content":"passed"}]}
]
```

接收方现在可以用 `sessions --agent example-agent` 查找会话，再用 `preview issue-42 --from example-agent --to codex` 和 `apply PLAN_ID` 拉取。源 agent 可以离线。保存协议文件不会自动创建中转站版本，也不要求来源方预先选接收方。如果已有私有会话格式，接入方负责把需要开放的会话映射到上述协议文件。

## 3. 接收、续写和重试

- **主动拉取**。接收端以自己的注册标识调用 `preview --from SOURCE --to TARGET SOURCE_SESSION`，再执行 `apply PLAN_ID`。MCP 对应 `preview_handoff` 和 `apply_handoff`。
- **离线指派**。发送方用 `capture` 和 `assign` 保存指派。接收端上线后用 `inbox --recipient TARGET` 查找，用 `claim ASSIGNMENT_ID --recipient TARGET` 领取。MCP 对应 `capture_version`、`assign_version`、`list_assignments` 和 `claim_assignment`。
- **实际接手**。`apply` 返回计划，`claim` 在 `plan` 字段中返回它。读取 `target_path`，核对 `meta.id == target_id` 和 `meta.cwd == workspace.cwd`，再在该目录通过自身运行时恢复历史。项目笔记和指派要求已随历史写入。接收端使用自己的权限和启动方式。`waiting_for_open` 表示记录已交付，应用内恢复由接收端完成。
- **继续转交**。保留目标 ID，追加新消息并原子替换同一会话文件。具有 `source` 能力的接收端可直接成为下一跳的来源。中转站保留任务和父计划关联。
- **失败与恢复**。调用方读取 `code`、`retryable`、`next_action`。不支持版本返回 `PROTOCOL_UNSUPPORTED`，未知标识返回 `AGENT_UNKNOWN`，方向不匹配返回 `CAPABILITY_UNSUPPORTED`。损坏的候选出现在 `find_sessions.warnings`。恢复目录权限或文件后可用原指派重试；重复领取复用同一目标。通用协议的 CLI `--open` 返回 `open_issue.code = MANUAL_OPEN_REQUIRED` 和已交付的目标路径。

## 恢复期间的原子发布

恢复在目录切换阶段临时撤掉协议目录的写权限，并在冻结后再次核对活动文件。无法实际禁止原子发布时，恢复返回 `RESTORE_CONFLICT`。客户端已成功发布新消息时，中转站返回可重试的 `RESTORE_CHANGED`，保留活动会话供原备份重试。

冻结期间，客户端的临时文件创建或原子替换可能遇到暂时的访问拒绝或路径不可用。客户端应保留待发送消息，重新解析注册时返回的绝对目录，读取当前会话后再追加和重试。只有原子替换成功才报告发布成功。恢复目录中的未完成临时文件不会转移成活动会话，保留在 `rollback_state` 中，避免旧文件句柄在切换后发布过期的副本。原回退目录的协议子目录保持只读，阻止已缓存旧目录句柄的客户端继续向退役存储报告写入成功。

独立客户端的可执行互操作测试位于 [tests/m8_protocol.py](../tests/m8_protocol.py)。切换窗口的确定性并发测试位于 `src/maintenance.rs` 的 Rust 测试模块。
