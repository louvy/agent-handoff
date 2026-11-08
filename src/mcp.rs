use crate::app::{Action, Cli};
use rmcp::{
    ServerHandler, ServiceExt,
    handler::server::{router::tool::ToolRouter, wrapper::Parameters},
    model::{CallToolResult, Implementation, ServerCapabilities, ServerConfig},
    schemars, tool, tool_handler, tool_router,
};
use serde::Deserialize;

#[derive(Clone)]
struct HandoffServer {
    config: Cli,
    tool_router: ToolRouter<Self>,
}
#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct Register {
    id: String,
    /// source, target or both
    role: String,
    protocol_version: u32,
}
#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct Find {
    /// Source ID from list_agents
    agent: Option<String>,
    project: Option<std::path::PathBuf>,
    query: Option<String>,
    limit: Option<usize>,
    offset: Option<usize>,
}
#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct Preview {
    source_ref: String,
    /// Source ID from list_agents; defaults to codex
    source_agent: Option<String>,
    /// Target ID from list_agents
    target: String,
    /// native is supported
    mode: Option<String>,
}
#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct Apply {
    plan_id: String,
}
#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct Get {
    handoff_id: String,
}
#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct Read {
    handoff_id: String,
    section: String,
    cursor: Option<usize>,
}

#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct Capture {
    source_ref: String,
    source_agent: String,
}
#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct Versions {
    task_id: Option<String>,
    limit: Option<usize>,
    offset: Option<usize>,
    cursor: Option<i64>,
}
#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct Prepare {
    version_id: String,
    target: String,
}
#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct Assign {
    version_id: String,
    recipient: String,
    instructions: String,
}
#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct Inbox {
    recipient: String,
    limit: Option<usize>,
    offset: Option<usize>,
    cursor: Option<i64>,
}
#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct Claim {
    assignment_id: String,
    recipient: String,
}
impl HandoffServer {
    async fn execute(&self, action: Action) -> CallToolResult {
        let mut cli = self.config.clone();
        cli.command = action;
        match tokio::task::spawn_blocking(move || crate::app::run(cli)).await {
            Ok(Ok(value)) => CallToolResult::structured(value),
            Ok(Err(err)) => CallToolResult::structured_error(crate::error::response(&err)),
            Err(err) => CallToolResult::structured_error(crate::error::response(&err.into())),
        }
    }
}
#[tool_router]
impl HandoffServer {
    #[tool(description = "列出可用 agent、来源和接收端标识及支持的接入协议版本")]
    async fn list_agents(&self) -> CallToolResult {
        self.execute(Action::Agents).await
    }
    #[tool(
        description = "自主接入本机协议。版本 1 使用独立的 sessions_dir 和每会话一个 JSON 文件，重复注册返回同一目录。"
    )]
    async fn register_agent(&self, Parameters(p): Parameters<Register>) -> CallToolResult {
        self.execute(Action::RegisterAgent {
            id: p.id,
            role: p.role,
            protocol_version: p.protocol_version,
        })
        .await
    }
    #[tool(
        description = "将已保存版本指派给指定接收端并持久化后续要求；成功表示待领取，接收端可以离线"
    )]
    async fn assign_version(&self, Parameters(p): Parameters<Assign>) -> CallToolResult {
        self.execute(Action::Assign {
            version: p.version_id,
            to: p.recipient,
            instructions: p.instructions,
        })
        .await
    }
    #[tool(description = "查询指定接收端的指派，包含待领取及已生成目标的状态和计划")]
    async fn list_assignments(&self, Parameters(p): Parameters<Inbox>) -> CallToolResult {
        self.execute(Action::Inbox {
            recipient: p.recipient,
            limit: p.limit.unwrap_or(100),
            offset: p.offset.unwrap_or(0),
            cursor: p.cursor,
        })
        .await
    }
    #[tool(
        description = "领取指定指派，幂等创建并发布同一目标会话；返回原生 ID 和打开步骤，实际接手需目标端验证"
    )]
    async fn claim_assignment(&self, Parameters(p): Parameters<Claim>) -> CallToolResult {
        self.execute(Action::Claim {
            assignment: p.assignment_id,
            recipient: p.recipient,
            open: false,
        })
        .await
    }
    #[tool(
        description = "按需从指定来源保存独立的结构化任务版本及笔记快照，无需指定接收端；返回版本 ID"
    )]
    async fn capture_version(&self, Parameters(p): Parameters<Capture>) -> CallToolResult {
        self.execute(Action::Capture {
            source: p.source_ref,
            from: p.source_agent,
        })
        .await
    }
    #[tool(description = "列出中转站已保存的独立版本，可按 task_id 查询版本及其父版本")]
    async fn list_versions(&self, Parameters(p): Parameters<Versions>) -> CallToolResult {
        self.execute(Action::Versions {
            task_id: p.task_id,
            limit: p.limit.unwrap_or(100),
            offset: p.offset.unwrap_or(0),
            cursor: p.cursor,
        })
        .await
    }
    #[tool(
        description = "仅从中转站的指定版本生成目标计划；来源记录和原笔记可以已不可访问，随后用 apply_handoff 发布"
    )]
    async fn prepare_handoff(&self, Parameters(p): Parameters<Prepare>) -> CallToolResult {
        self.execute(Action::Prepare {
            version: p.version_id,
            to: p.target,
        })
        .await
    }
    #[tool(
        description = "查找内置或已注册来源的本地会话。query 为 ID、标题或可见正文的原样子串。返回 total、next_cursor，下一页将 next_cursor 作为 offset。候选有歧义时先让用户选择，返回精确来源引用和工作目录",
        annotations(read_only_hint = true, open_world_hint = false)
    )]
    async fn find_sessions(&self, Parameters(p): Parameters<Find>) -> CallToolResult {
        self.execute(Action::Sessions {
            agent: p.agent.unwrap_or_else(|| "codex".into()),
            project: p.project,
            query: p.query,
            limit: p.limit.unwrap_or(20),
            offset: p.offset.unwrap_or(0),
        })
        .await
    }
    #[tool(description = "预览跨客户端原生接手，固定历史截止位置、worktree 和记忆；返回待执行计划")]
    async fn preview_handoff(&self, Parameters(p): Parameters<Preview>) -> CallToolResult {
        if p.mode.as_deref().is_some_and(|m| m != "native") {
            return CallToolResult::structured_error(crate::error::response(&anyhow::anyhow!(
                "只支持 native 模式"
            )));
        }
        self.execute(Action::Preview {
            source: p.source_ref,
            from: p.source_agent.unwrap_or_else(|| "codex".into()),
            to: p.target,
            mode: "native".into(),
        })
        .await
    }
    #[tool(
        description = "执行预览计划，幂等创建目标会话并返回原生 ID 和人工打开步骤",
        annotations(
            read_only_hint = false,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    async fn apply_handoff(&self, Parameters(p): Parameters<Apply>) -> CallToolResult {
        self.execute(Action::Apply {
            plan: p.plan_id,
            open: false,
        })
        .await
    }
    #[tool(
        description = "查询交接状态、原生 ID、父计划和工作目录",
        annotations(read_only_hint = true, open_world_hint = false)
    )]
    async fn get_handoff(&self, Parameters(p): Parameters<Get>) -> CallToolResult {
        self.execute(Action::Status { id: p.handoff_id }).await
    }
    #[tool(
        description = "有界读取交接或独立版本 ID 对应的 transcript 或 memory；以 next_cursor 继续读取",
        annotations(read_only_hint = true, open_world_hint = false)
    )]
    async fn read_context(&self, Parameters(p): Parameters<Read>) -> CallToolResult {
        if !matches!(p.section.as_str(), "transcript" | "memory") {
            return CallToolResult::structured_error(crate::error::response(&anyhow::anyhow!(
                "不支持的输入参数"
            )));
        }
        self.execute(Action::Read {
            id: p.handoff_id,
            section: p.section,
            cursor: p.cursor.unwrap_or(0),
        })
        .await
    }
}
#[tool_handler(router = self.tool_router)]
impl ServerHandler for HandoffServer {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(Implementation::new(
                "agent-handoff",
                env!("CARGO_PKG_VERSION"),
            ))
            .with_instructions(
                "先查找来源，再预览及执行。原生打开后须核对工作树。仅供本机已授权的会话接手。",
            )
    }
}
// Detect transport EOF before the MCP service drains outstanding handlers.
struct Input {
    inner: tokio::io::Stdin,
    disconnected: std::sync::Arc<std::sync::atomic::AtomicBool>,
}
impl tokio::io::AsyncRead for Input {
    fn poll_read(
        mut self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        buf: &mut tokio::io::ReadBuf<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        let before = buf.filled().len();
        let had_capacity = buf.remaining() > 0;
        let result = std::pin::Pin::new(&mut self.inner).poll_read(cx, buf);
        if matches!(result, std::task::Poll::Ready(_))
            && had_capacity
            && buf.filled().len() == before
        {
            self.disconnected
                .store(true, std::sync::atomic::Ordering::Relaxed);
        }
        result
    }
}
pub(crate) async fn serve(mut config: Cli) -> anyhow::Result<()> {
    let disconnected = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    config.disconnected = Some(disconnected.clone());
    HandoffServer {
        config,
        tool_router: HandoffServer::tool_router(),
    }
    .serve((
        Input {
            inner: tokio::io::stdin(),
            disconnected,
        },
        tokio::io::stdout(),
    ))
    .await?
    .waiting()
    .await?;
    Ok(())
}
