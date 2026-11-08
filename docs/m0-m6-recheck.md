# Validation / 验收与复现

The CLI and MCP server are tested against temporary native session stores and real Git worktrees. DeepSeek tests load the installed Harness SDK. Desktop opening and continuing a task are separate, interactive checks.

## What was verified / 验证范围

| 来源 | 接收端 | 主动拉取 | 离线指派后领取 |
|---|---|---|---|
| Codex | Claude Desktop Code、DeepSeek Harness | 2/2 | 2/2 |
| Claude Desktop Code | Codex、DeepSeek Harness | 2/2 | 2/2 |
| DeepSeek Harness | Codex、Claude Desktop Code | 2/2 | 2/2 |

自动化验收通过 CLI 和真实 stdio MCP 获取目标原生记录，按目标 ID 重新采集，核对历史、工具结果、笔记、指派要求、父计划和原工作目录。测试也核对源记录及原 worktree 的暂存状态。前台接手曾在 macOS 的三个实际客户端验证六个方向和 Codex → Claude → DeepSeek → Codex 往返；Claude Desktop 的目录信任、选择会话和输入步骤需要用户操作。自动化测试不会替用户点击客户端。

## State and recovery / 状态与恢复

- **缺失或损坏的来源。** 区分会话不存在、引用不唯一、会话损坏、缺少工作目录、非 Git 目录和工作树变化。健康候选与部分搜索警告可同时返回。
- **接收失败与安全重试。** 验证目标冲突、不可读、SDK 不可用和启动失败。恢复环境后复用同一指派、计划和原生目标；保留已追加历史。已发布目标丢失、被清空或替换时应拒绝继续打开。
- **备份与并发。** 同一状态库的旧备份不会索引后来创建的计划。反复执行版本查询后，三个目标计划的文件清单及摘要保持一致；放回最新库后，三个原 ID 均可发布并回读。还覆盖数据库缺失、事务回滚、并发领取、准备期间中断和提交结果不明时的目录保留。
- **兼容范围。** Codex 和 Claude 使用未压缩 JSONL；DeepSeek 使用 Harness 0.2.2-rc.1 的 v4 格式。模型私有状态、媒体跨端保真、其他 SDK 版本及其他操作系统需单独验证。

## 自主接入验收

独立 Python 客户端在仓库外的临时目录启动，通过 CLI 注册双向角色。已运行的 stdio MCP 服务能发现该注册并向它指派 Codex 版本。客户端从原 worktree 读取分支和仅该工作树包含的文件内容，把结果写回目标会话。随后从 MCP 主动拉取续写到 Claude 的原生存储，核对父计划、历史、工具结果、笔记和指派要求。此项覆盖协议文件消费和原生存储交付，Claude 的前台窗口由前述桌面验收覆盖。

同组测试还覆盖两个外部 agent 之间的交接、重复及并发注册、角色越权、版本不兼容、标识冲突、损坏记录、缺失工作目录、符号链接、接收目录恢复后复用指派，以及发布中断后保留新增消息。[协议用例](../tests/m8_protocol.py)

## 自主接入缺口修复复验

本轮以 `6a1b21f` 为修复基线。新增两个回归用例先复现了历史不完整仍放行、权限错误被重分类的问题。

| 场景 | 完成判据 |
|---|---|
| 已发布的外部会话被截断、删除首条消息、改写正文或工具结果、删除交接要求 | CLI/MCP 的 apply 和 claim 均返回 `TARGET_INVALID`，保留现有目标和原计划 ID。 |
| 恢复完整历史，追加消息并修改标题和 JSON 排版 | 原指派与计划继续可用，已追加进度保持完整。 |
| 外部来源会话暂时不可读 | 按 ID 或精确路径调用 capture，或通过 MCP preview 拉取，返回与 find_sessions 警告相同的 `ACCESS_DENIED`、恢复指引和可重试性。 |
| 恢复读取权限 | 可重新采集同一来源，原指派领取继续复用目标 ID 和内容。 |
| 外部 agent 与三个内置端双向交接 | 主动拉取和离线指派共 12 条链路核对父计划、原 worktree、历史、工具结果、笔记和后续要求。 |

本轮自动化复验覆盖真实 CLI、stdio MCP 和本地会话存储，未重新执行前台窗口恢复操作。

## Reproduce / 本地复现

Start from the repository root on macOS with Git, Rust 1.96+, Python 3 and zstd. The full suite requires Node.js 24.9+ and the tested Harness package on PATH. For example, install the SDK with `npm install --global @deepseek-ai/dsh@0.2.2-rc.1`.

```sh
cargo fmt --check
cargo build --locked
python3 -m unittest discover -s tests -p 'm*.py' -v
python3 tests/acceptance_matrix.py
```

The regression suite contains 44 tests. The matrix runs 64 checks and observations, including the 12 cross-client pull and assignment flows. It writes a JSON report to stdout. The held-database-lock case allows 30 seconds for its MCP response, records CLI/MCP elapsed times, and verifies capture succeeds after the lock is released. Other requests retain the default 15-second timeout. The commands use isolated fixture data; no live conversations or credentials are required.

Use [the backup and recovery tests](../tests/m7_boundaries.py) for the database restore scenarios. To reproduce the manual worktree check, run `python3 scripts/m0_fixture.py` to generate a disposable repository and native-test inputs under the ignored `.m0` directory. After continuing a task in the real target client, use `python3 scripts/m0_verify.py --help` to check an exported native transcript against that fixture.
