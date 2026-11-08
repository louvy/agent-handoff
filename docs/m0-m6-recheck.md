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
- **接收失败与安全重试。** 验证目标冲突、不可读、SDK 不可用和启动失败。恢复环境后复用同一指派、计划和原生目标；保留已追加历史。已发布目标丢失、被清空或替换时应拒绝继续打开。内置目标还会保存交付时的消息清单，重试和领取时把目标重新解码并核对角色与内容前缀：删除、截断或改写已交付消息返回 `TARGET_INVALID`，客户端追加消息或改写标题、时间戳、模型和 JSON 排版不受影响。旧版本创建的计划没有清单，会从计划目录保存的原生产物重建基线并继续校验；产物缺失时返回 `BASELINE_MISSING`。
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

## 内置目标的新旧计划复验

Codex、Claude Desktop Code、DeepSeek Harness 各验证两种计划。新计划使用交付时的消息清单，旧计划先核对原生产物的 SHA-256，再从该产物读取历史。另用旧提交 `a20e631` 的二进制实际创建、领取三端指派，再用当前二进制重试原 ID。删除旧计划的 staging 目录后仍可验证。DeepSeek 使用官方 SDK 写入和回读结构合法的 v4 会话。

| 场景 | 三端新旧计划的实际结果 |
|---|---|
| 删除中间一条助手消息、改写工具结果、移除指派要求 | 修改后的会话均可重新采集。CLI `apply` 和 MCP `claim_assignment` 都返回 `TARGET_INVALID`，目标文件保持原样。 |
| 恢复历史后追加消息、重新序列化，Claude 和 DeepSeek 同时改标题 | 追加内容可从原 worktree 对应的会话回读。用原指派再次领取，计划 ID、目标 ID 和文件内容均保留。 |
| 文件已交付且已续写，数据库状态仍为 `previewed` | 复用已有目标，状态恢复到 `waiting_for_open`，续写保留。 |
| 移走基线、破坏摘要、暂时撤掉读权限 | 分别返回 `BASELINE_MISSING`、`PLAN_CORRUPT`、`ACCESS_DENIED`。放回原文件并恢复权限后可用原 ID 继续。 |

本轮运行 47 项自动化测试和组合矩阵的 64 项检查与观察。三端六个有向组合的主动拉取和离线指派共 12 条链路全部通过。验证覆盖 CLI、stdio MCP、Git worktree 和原生会话存储，桌面窗口沿用此前的人工验收记录。

专项用例可单独复现。常规运行会构造无历史清单的旧格式计划。将可选环境变量 `HANDOFF_LEGACY_BINARY` 指向从 `a20e631` 构建的 handoff 可执行文件，可让旧程序生成升级样本。[专项测试](../tests/m9_builtin_history.py)

```sh
cargo build --locked
PYTHONPATH=tests python3 -m unittest -v m9_builtin_history
```

## 状态生命周期专项验收

开发分支 `feat/handoff-state-lifecycle` 覆盖方案第 2–6 步。新增用例位于 [m10_state.py](../tests/m10_state.py)，使用独立临时目录、真实 Git worktree、CLI 和 stdio MCP。

- 查询不会初始化空状态目录、迁移已有库或删除准备目录，数据库缺失保留恢复指引。
- 删除笔记后可列出旧绑定并解绑，已冻结笔记继续回读；依赖和构建目录的超限 Markdown 被跳过。
- 旧备份与最新备份往返恢复后，后建计划仍用原 ID 执行。恢复保留协议客户端的新续写，生成回退备份，损坏备份被拒绝。
- 旧表结构可查询并在下次写入迁移，来源查询使用索引。并发相同采集复用版本，HEAD 改变产生新版本。新增记录期间按游标翻页无重复，收件箱分页可用。
- 待领取指派阻止归档。GC 默认预览，执行后保留未知提交目录和原生目标，会删除符合条件的归档计划。清理中断后下次写入恢复目录，活动分叉保留祖先计划与版本。

在本机 SQLite 隔离样本中，20,000 条约 2 KiB 的版本记录按来源查询，JSON 条件扫描约 21.6 ms，生成列索引查询约 0.024 ms。该样本用于确认查询路径，实际用户数据的延迟取决于磁盘和记录规模。

最终完整回归通过 54 项测试，三端矩阵的 64 项检查与观察全部完成，其中主动拉取和指派共 12 条链路通过。构建、Rust 格式和文档链接检查通过。回归中发现既有中断测试在监督进程已回收 SDK 后重复发送终止信号，已改为断言 SDK 确实退出，并通过专项及完整复验。当前环境缺少 Clippy 组件，未执行 Clippy 检查。桌面窗口沿用历史验收证据，本轮验证协议、状态及原生存储。

## main 独立复验：方案 1–6（`c78af41`）

结论：**未整体验收通过**。在 macOS 隔离状态目录、Git worktree、真实 CLI 和 stdio MCP 上，格式检查、锁定依赖构建和 54 项回归通过；组合矩阵完成 64 项检查与观察，Codex、Claude Desktop、DeepSeek 六个方向的拉取和指派共 12 条全部通过。本轮验证到原生会话存储，没有重新操作桌面窗口。

| 项 | 本轮证据 | 判定 |
| --- | --- | --- |
| 1 历史完整性 | 三个内置目标的新旧计划通过删改、合法续写、恢复后复用原 ID 回归。 | 通过 |
| 2 只读查询 | 空状态下 MCP 能发现 Codex 会话且不创建状态目录；已有库的业务记录和准备目录保留。但已有 WAL 模式数据库缺少辅助文件时，第一次 MCP 查询改变了文件清单。 | 部分通过 |
| 3 笔记 | 笔记改名后可列出并解绑；再次采集成功，另一个 worktree 的绑定和旧快照不变。依赖目录跳过测试通过。 | 通过 |
| 4 备份恢复 | 并发指派与备份得到一致快照，回退后可用原 ID 领取；正常旧、新库往返通过。空目录恢复、协议历史冲突及失败回滚存在下列缺口。 | 未通过 |
| 5 查询与复用 | 旧表迁移、分页、并发采集去重、HEAD 变化回归通过。另用 12,000 条约 2 KiB 的版本记录测来源查询：JSON 扫描中位数约 24 ms，命中 `version_source` 后约 0.006 ms（本机热缓存，非端到端延迟）。 | 已测路径通过 |
| 6 归档与 GC | 待领取指派、活动分叉祖先、客户端会话和未知提交目录的保护回归通过。另在同一状态恢复旧备份后实际执行 GC，后建计划文件保留；放回最新备份，原指派领取到同一计划。 | 已测路径通过 |

独立复现（均在临时数据上进行）：

1. **P1 · 协议会话恢复误报成功。** 给已交付协议会话追加一条有效消息并备份，将本地文件退回追加前的有效内容，再恢复新备份：`restore` 和原 `claim` 都成功，但备份里的追加消息没有回来。把本地文件清空再恢复，同样报告成功，原 `claim` 随后返回 `TARGET_INVALID`。恢复应区分内容相同、哪一侧是合法续写，以及损坏或分叉；不能一律跳过已存在文件。
2. **P1 · 已注册但尚无会话的 agent 不能从备份完整恢复。** 注册双向 agent、创建待领取指派并备份，移开整个状态目录，恢复到原路径：注册记录在库中，但协议会话目录未重建，原 `claim` 返回 `FILE_UNAVAILABLE`。重新注册同一 agent 后原指派可继续。
3. **P1 · 失败的恢复会修改目标。** 在已备份计划中移走 `.handoff-commit-intent`，让后处理的 `transcript.txt` 与备份冲突。执行 `restore` 返回 `RESTORE_CONFLICT`，但原先缺少的标记已经写回。备份暂存校验不等于对目标的原子切换。另测当前 SQLite 主文件已损坏时，原地恢复有效备份会返回 `STATE_CORRUPT`；移开原状态目录和原地修复是不同场景。
4. **P1 · 写操作等待文件锁没有期限。** 独立进程持有 `.handoff-operation.lock` 时，CLI 写入在 12 秒后仍等待；同一时段 MCP 的版本查询约 4 ms 返回。结束持锁进程后，排队的 CLI 和 MCP 写入都返回原版本 ID。另测客户端关闭 stdio 后，排队写入的 MCP 进程在 2 秒后仍在，释放锁后正常退出。对照：SQLite 锁冲突在 CLI/MCP 上约 10.4 秒返回可重试的 `STATE_BUSY`；慢启动器不阻塞其他指派，DeepSeek 超时及子进程回收回归通过。没有观察到循环死锁，但文件锁不能保证有界等待。
5. **P2 · 只读查询仍有文件系统副作用。** 关闭最后一个写入连接，确认仅有 `handoff.sqlite3`，调用真实 MCP `get_handoff` 后出现 `-wal` 和 `-shm`。在尚无这两个文件的独立样本中，将状态目录设为只读后，CLI `status` 返回 `STATE_UNAVAILABLE`。现有测试的快照明确忽略了这两个文件，不能证明文件清单不变；未观察到查询改写业务记录。

## 恢复一致性复验（`fix/handoff-restore-consistency`）

保留上面的 `c78af41` 独立复验记录，五项缺口由本分支修复。新增 [m11_restore.py](../tests/m11_restore.py) 的 12 个隔离用例，通过真实 CLI、stdio MCP、SQLite 和 Git worktree 执行。另覆盖无 `target_agent` 字段的旧计划、异常来源候选的原样恢复，以及结构有效但交付历史不完整的协议目标被拒绝。

| 缺口 | 修复与复验 |
|---|---|
| 新备份的协议历史未恢复 | 备份较新时补回消息，本地较新时保留续写。空白或损坏的本地文件用有效备份修复。两侧分叉返回 `RESTORE_CONFLICT`，核对完整文件清单、摘要和权限保持不变。原指派复用同一目标 ID。 |
| 空 agent 目录未重建 | 移开整个状态目录后，在原路径恢复。已注册但没有会话的 agent 目录直接可用，原指派领取成功。 |
| 冲突返回前发生部分写入 | 恢复在暂存状态中校验计划文件和协议历史，全部通过后切换目录。缺少提交标记且稍后的正文文件冲突时，原目录未被补写。当前数据库损坏或清空时，原始内容保留在 `rollback_state`，备份恢复后原计划可执行。另模拟原目录已移走但新目录尚未启用的中断，下一次写入恢复原状态。 |
| 文件锁等待无上限 | 新旧两种文件锁分别被独立进程持有时，设置 1 秒等待期限，CLI 有界返回可重试的 `STATE_BUSY`。MCP 客户端关闭 stdin 后，排队写入取消，进程在 2 秒内退出。释放锁后原版本 ID 可继续使用。默认期限为 5 秒。 |
| 只读查询创建辅助文件 | 新测试核对源目录全部文件及权限，包含 WAL、SHM。没有辅助文件的库和只读状态目录均可查询。有活动写入连接和已提交 WAL 时可见新版本，未提交的回滚日志记录保持不可见，源文件清单和摘要不变。原有 m10 快照也已取消对辅助文件的排除。 |

旧备份与 GC 专项再次实际执行。先备份归档状态，再创建新指派与计划并保存最新备份。恢复旧库后执行 GC，旧归档计划被选中清理，后建计划的文件摘要保持一致。放回最新备份，原指派 ID 领取同一计划。较慢启动器和 DeepSeek 子进程回收继续由既有回归覆盖。

最终锁定依赖构建、Rust 格式检查和 66 项自动化测试通过。三端组合矩阵完成 64 项检查与观察，其中六个有向组合的拉取和指派共 12 条链路通过。旧计划兼容补充后，12 个恢复专项用例再次通过。只读查询当前使用完整数据库的私有副本以避免源目录写入，大状态库的端到端延迟尚未量测。本轮验证到协议、状态库和原生会话存储，桌面窗口沿用历史验收证据。恢复仍要求原状态路径，升级后应重启各 MCP 服务以统一锁协议。

## Reproduce / 本地复现

Start from the repository root on macOS with Git, Rust 1.96+, Python 3 and zstd. The full suite requires Node.js 24.9+ and the tested Harness package on PATH. For example, install the SDK with `npm install --global @deepseek-ai/dsh@0.2.2-rc.1`.

```sh
cargo fmt --check
cargo build --locked
python3 -m unittest discover -s tests -p 'm*.py' -v
python3 tests/acceptance_matrix.py
```

The regression suite contains 66 tests. The matrix runs 64 checks and observations, including the 12 cross-client pull and assignment flows. It writes a JSON report to stdout. The held-database-lock case allows 30 seconds for its MCP response, records CLI/MCP elapsed times, and verifies capture succeeds after the lock is released. Other requests retain the default 15-second timeout. The commands use isolated fixture data; no live conversations or credentials are required.

Use [the backup and recovery tests](../tests/m7_boundaries.py) for the database restore scenarios. To reproduce the manual worktree check, run `python3 scripts/m0_fixture.py` to generate a disposable repository and native-test inputs under the ignored `.m0` directory. After continuing a task in the real target client, use `python3 scripts/m0_verify.py --help` to check an exported native transcript against that fixture.
