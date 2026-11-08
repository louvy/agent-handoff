use crate::agent::{discover, load_common};
use anyhow::{Context, Result, ensure};
use clap::{Parser, Subcommand};
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Write,
    os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    path::{Path, PathBuf},
    process::Command,
};
use txcript::{
    Transcript,
    common::{Block, Message, Meta, Role},
};

#[derive(Parser)]
#[command(version, about = "在原 Git worktree 接手本地编码会话")]
#[derive(Clone)]
pub(crate) struct Cli {
    #[arg(long, global = true, env = "HANDOFF_STATE_DIR")]
    pub(crate) state_dir: Option<PathBuf>,
    #[arg(skip)]
    pub(crate) registered: crate::protocol::Registry,
    #[arg(long, global = true)]
    pub(crate) codex_root: Option<PathBuf>,
    #[arg(long, global = true)]
    pub(crate) claude_root: Option<PathBuf>,
    #[arg(long, global = true)]
    pub(crate) dsh_root: Option<PathBuf>,
    #[arg(long, global = true)]
    pub(crate) dsh_package: Option<PathBuf>,
    /// Maximum duration of each DeepSeek SDK operation.
    #[arg(long, global = true, default_value_t = 30, value_parser = clap::value_parser!(u64).range(1..))]
    pub(crate) adapter_timeout_seconds: u64,
    /// Maximum wait for another state writer.
    #[arg(long, global = true, default_value_t = 5, value_parser = clap::value_parser!(u64).range(1..))]
    pub(crate) lock_timeout_seconds: u64,
    #[arg(skip)]
    pub(crate) disconnected: Option<std::sync::Arc<std::sync::atomic::AtomicBool>>,
    #[command(subcommand)]
    pub(crate) command: Action,
}
#[derive(Subcommand, Clone)]
pub(crate) enum Action {
    #[command(name = "__adapter-supervisor", hide = true)]
    AdapterSupervisor {
        #[arg(long)]
        package: PathBuf,
        #[arg(long)]
        owner_pid: u32,
        #[arg(long)]
        timeout_seconds: u64,
    },
    Mcp,
    Backup {
        path: PathBuf,
    },
    Restore {
        path: PathBuf,
    },
    Archive {
        task_id: String,
    },
    Gc {
        #[arg(long, default_value = "30d")]
        older_than: String,
        #[arg(long)]
        execute: bool,
    },
    /// Discover built-in and registered local agents.
    Agents,
    /// Join the local session exchange using a versioned protocol.
    RegisterAgent {
        #[arg(long)]
        id: String,
        #[arg(long)]
        role: String,
        #[arg(long)]
        protocol_version: u32,
    },
    Sessions {
        #[arg(long, default_value = "codex")]
        agent: String,
        #[arg(long)]
        project: Option<PathBuf>,
        #[arg(long)]
        query: Option<String>,
        #[arg(long, default_value_t = 20)]
        limit: usize,
        #[arg(long, default_value_t = 0)]
        offset: usize,
    },
    Memory {
        #[command(subcommand)]
        action: MemoryAction,
    },
    Preview {
        source: String,
        #[arg(long, default_value = "codex")]
        from: String,
        #[arg(long, default_value = "claude-desktop")]
        to: String,
        #[arg(long, default_value = "native", value_parser = ["native"])]
        mode: String,
    },
    Capture {
        source: String,
        #[arg(long, default_value = "codex")]
        from: String,
    },
    Versions {
        #[arg(long)]
        task_id: Option<String>,
        #[arg(long, default_value_t = 100)]
        limit: usize,
        #[arg(long, default_value_t = 0)]
        offset: usize,
        #[arg(long)]
        cursor: Option<i64>,
    },
    Prepare {
        version: String,
        #[arg(long)]
        to: String,
    },
    Assign {
        version: String,
        #[arg(long)]
        to: String,
        #[arg(long)]
        instructions: String,
    },
    Inbox {
        #[arg(long)]
        recipient: String,
        #[arg(long, default_value_t = 100)]
        limit: usize,
        #[arg(long, default_value_t = 0)]
        offset: usize,
        #[arg(long)]
        cursor: Option<i64>,
    },
    Claim {
        assignment: String,
        #[arg(long)]
        recipient: String,
        #[arg(long)]
        open: bool,
    },
    Apply {
        plan: String,
        #[arg(long)]
        open: bool,
    },
    Status {
        id: String,
    },
    Read {
        id: String,
        #[arg(long, value_parser = ["memory", "transcript"])]
        section: String,
        #[arg(long, default_value_t = 0)]
        cursor: usize,
    },
}
#[derive(Subcommand, Clone)]
pub(crate) enum MemoryAction {
    List {
        #[arg(long)]
        project: PathBuf,
    },
    Unbind {
        #[arg(long)]
        project: PathBuf,
        #[arg(long)]
        path: PathBuf,
    },

    Bind {
        #[arg(long)]
        project: PathBuf,
        #[arg(long)]
        path: PathBuf,
    },
}

#[derive(Serialize, Deserialize, Clone)]
struct Workspace {
    cwd: PathBuf,
    root: PathBuf,
    git_dir: PathBuf,
    head: String,
}
#[derive(Serialize, Deserialize, Clone)]
struct Note {
    path: PathBuf,
    digest: String,
    text: String,
}
#[derive(Serialize, Deserialize)]
struct Plan {
    id: String,
    task_id: String,
    source_id: String,
    #[serde(default = "default_source")]
    source_agent: String,
    #[serde(default = "default_target")]
    target_agent: String,
    #[serde(default)]
    parent_plan_id: Option<String>,
    #[serde(default)]
    version_id: Option<String>,
    #[serde(default = "verify_source_default")]
    verify_source: bool,
    source: PathBuf,
    source_digest: String,
    workspace: Workspace,
    memory: Vec<Note>,
    target_id: String,
    target: PathBuf,
    prepared: PathBuf,
    prepared_digest: String,
    transcript: PathBuf,
    #[serde(default)]
    history: Option<PathBuf>,
    #[serde(default)]
    history_digest: Option<String>,
    losses: Vec<String>,
}
fn verify_source_default() -> bool {
    true
}

#[derive(Serialize, Deserialize)]
struct Version {
    format_version: u32,
    id: String,
    task_id: String,
    parent_version_id: Option<String>,
    parent_plan_id: Option<String>,
    created_at: String,
    source_agent: String,
    source_id: String,
    source: PathBuf,
    source_digest: String,
    workspace: Workspace,
    memory: Vec<Note>,
    meta: Meta,
    messages: Vec<Message>,
}
fn version_view(v: &Version) -> Value {
    json!({"id":v.id,"task_id":v.task_id,"parent_version_id":v.parent_version_id,
      "parent_plan_id":v.parent_plan_id,"created_at":v.created_at,"source_agent":v.source_agent,
      "source_id":v.source_id,"source_digest":v.source_digest,"workspace":v.workspace,
      "message_count":v.messages.len(),"memory":v.memory.iter().map(|n| json!({"path":n.path,"digest":n.digest})).collect::<Vec<_>>()})
}
fn load_version(db: &Connection, id: &str) -> Result<Version> {
    let body: String = db
        .query_row("SELECT body FROM versions WHERE id=?", [id], |r| r.get(0))
        .context("找不到独立版本")?;
    let v: Version = serde_json::from_str(&body)?;
    ensure!(v.format_version == 1, "不支持的独立版本格式");
    Ok(v)
}
fn capture(cli: &Cli, db: &mut Connection, source: &str, source_agent: &str) -> Result<Version> {
    let (found, warnings) = discover(cli, source_agent)?;
    let matches: Vec<_> = found
        .iter()
        .filter(|s| s.meta.id == source || s.reference.to_string_lossy() == source)
        .collect();
    if matches.is_empty() {
        if let Some(warning) = warnings.iter().find(|w| {
            w["source_ref"].as_str() == Some(source) || w["session_id"].as_str() == Some(source)
        }) {
            return Err(crate::error::from_response(&warning["issue"])?);
        }
        return Err(crate::error::fault(
            "SESSION_NOT_FOUND",
            "指定来源会话不存在",
            "重新查找 sessions 并选择精确引用，或使用中转站已保存版本。",
            false,
        ));
    }
    if matches.len() > 1 {
        return Err(crate::error::fault(
            "SESSION_AMBIGUOUS",
            "来源会话 ID 对应多条记录",
            "查询 sessions，选择唯一的 source_ref 精确路径。",
            false,
        ));
    }
    let item = matches[0];
    let before = fs::read(&item.reference)?;
    let common = load_common(cli, source_agent, &item.meta.id, &before).map_err(|error| {
        if error.downcast_ref::<crate::error::Fault>().is_some() {
            return error;
        }
        crate::error::fault(
            "SOURCE_INVALID",
            "来源会话内容无法读取",
            "选择有效来源或中转站已保存的版本。",
            false,
        )
    })?;
    let ws = workspace(Path::new(
        common.meta.cwd.as_deref().context("来源缺少 cwd")?,
    ))?;
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let memory = read_notes(&tx, &ws.root)?;
    ensure!(
        before == fs::read(&item.reference)?,
        "来源在读取时变化，请重试"
    );
    let mut task_id = format!("{source_agent}:{}", item.meta.id);
    let mut parent_plan_id = None;
    let mut parent_version_id = None;
    {
        let prior: Option<String> = tx.query_row("SELECT body FROM plans WHERE target_id=? AND target_path=? AND status != 'previewed' ORDER BY rowid DESC LIMIT 1",params![item.meta.id,item.reference.to_string_lossy()],|r|r.get(0)).optional()?;
        if let Some(body) = prior {
            let prior: Plan = serde_json::from_str(&body)?;
            task_id = prior.task_id;
            parent_plan_id = Some(prior.id);
            parent_version_id = prior.version_id;
        }
        let prior:Option<String>=tx.query_row("SELECT body FROM versions WHERE source_agent=? AND source_id=? AND source_path=? ORDER BY rowid DESC LIMIT 1",params![source_agent,item.meta.id,item.reference.to_string_lossy()],|r|r.get(0)).optional()?;
        if let Some(body) = prior {
            let prior: Version = serde_json::from_str(&body)?;
            if prior.source_digest == digest(&before)
                && prior.parent_plan_id == parent_plan_id
                && serde_json::to_value(&prior.workspace)? == serde_json::to_value(&ws)?
                && serde_json::to_value(&prior.memory)? == serde_json::to_value(&memory)?
            {
                tx.execute("DELETE FROM archives WHERE task_id=?", [&prior.task_id])?;
                tx.commit()?;
                return Ok(prior);
            }
            task_id = prior.task_id;
            parent_version_id = Some(prior.id);
        }
    }
    tx.execute("DELETE FROM archives WHERE task_id=?", [&task_id])?;
    let v = Version {
        format_version: 1,
        id: uuid::Uuid::new_v4().to_string(),
        task_id,
        parent_version_id,
        parent_plan_id,
        created_at: chrono::Utc::now().to_rfc3339(),
        source_agent: source_agent.into(),
        source_id: item.meta.id.clone(),
        source: item.reference.clone(),
        source_digest: digest(&before),
        workspace: ws,
        memory,
        meta: common.meta,
        messages: common.body,
    };
    tx.execute(
        "INSERT INTO versions(id,body) VALUES(?,?)",
        params![v.id, serde_json::to_string(&v)?],
    )?;
    tx.commit()?;
    Ok(v)
}
fn default_source() -> String {
    "codex".into()
}
fn default_target() -> String {
    "claude-desktop".into()
}
fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn role_name(role: Role) -> &'static str {
    match role {
        Role::User => "user",
        Role::Assistant => "assistant",
    }
}
#[derive(Serialize, Deserialize)]
struct HistoryMessage {
    role: String,
    digest: String,
}
#[derive(Serialize, Deserialize)]
struct HistoryManifest {
    format_version: u32,
    messages: Vec<HistoryMessage>,
}
impl HistoryManifest {
    fn from_messages(messages: &[Message]) -> Result<Self> {
        let entries = messages
            .iter()
            .map(|message| {
                Ok(HistoryMessage {
                    role: role_name(message.role).into(),
                    digest: digest(&serde_json::to_vec(&message.content)?),
                })
            })
            .collect::<Result<_>>()?;
        Ok(Self {
            format_version: 1,
            messages: entries,
        })
    }
}

fn baseline_bytes(path: &Path, expected_digest: &str) -> Result<Vec<u8>> {
    let bytes = fs::read(path).map_err(|error| match error.kind() {
        std::io::ErrorKind::NotFound => crate::error::fault(
            "BASELINE_MISSING",
            "计划缺少交付历史基线",
            "从备份恢复原计划产物后，用同一计划或指派 ID 重试。",
            true,
        ),
        _ => error.into(),
    })?;
    ensure!(
        digest(&bytes) == expected_digest,
        crate::error::plan_corrupt()
    );
    Ok(bytes)
}

// Fingerprint the target's delivered roles and content. Titles, timestamps,
// model attribution and additional turns belong to the receiving client.
fn verify_history(cli: &Cli, plan: &Plan, messages: &[Message]) -> Result<()> {
    let manifest = match (&plan.history, &plan.history_digest) {
        (Some(path), Some(expected)) => {
            let bytes = baseline_bytes(path, expected)?;
            serde_json::from_slice::<HistoryManifest>(&bytes)
                .map_err(|_| crate::error::plan_corrupt())?
        }
        (None, None) => {
            let bytes = baseline_bytes(&plan.prepared, &plan.prepared_digest)?;
            let delivered = crate::agent::read_prepared(
                cli,
                &plan.target_agent,
                &plan.target_id,
                &plan.target,
                &bytes,
            )?;
            ensure!(
                delivered.meta.id == plan.target_id
                    && delivered.meta.cwd.as_deref() == plan.workspace.cwd.to_str(),
                crate::error::plan_corrupt()
            );
            HistoryManifest::from_messages(&delivered.body)?
        }
        _ => return Err(crate::error::plan_corrupt()),
    };
    ensure!(
        manifest.format_version == 1 && !manifest.messages.is_empty(),
        crate::error::plan_corrupt()
    );
    ensure!(
        messages.len() >= manifest.messages.len(),
        crate::error::target_invalid()
    );
    for (message, delivered) in messages.iter().zip(manifest.messages) {
        ensure!(
            role_name(message.role) == delivered.role
                && digest(&serde_json::to_vec(&message.content)?) == delivered.digest,
            crate::error::target_invalid()
        );
    }
    Ok(())
}
fn home() -> Result<PathBuf> {
    Ok(std::env::var_os("HOME").context("HOME 未设置")?.into())
}
pub(crate) fn private_dir(path: &Path) -> Result<()> {
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(path)?;
    Ok(())
}
fn write_new(path: &Path, data: &[u8]) -> Result<()> {
    let mut f = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    f.write_all(data)?;
    f.sync_all()?;
    Ok(())
}
fn git(cwd: &Path, args: &[&str]) -> Result<String> {
    let out = Command::new("git").arg("-C").arg(cwd).args(args).output()?;
    ensure!(
        out.status.success(),
        crate::error::fault(
            "WORKSPACE_INVALID",
            "工作目录无法作为 Git 工作区读取",
            "检查原目录的 Git 元数据和访问权限，恢复后重试。",
            true
        )
    );
    Ok(String::from_utf8(out.stdout)?.trim().into())
}
fn workspace(cwd: &Path) -> Result<Workspace> {
    let cwd = cwd.canonicalize().map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            crate::error::fault(
                "WORKSPACE_MISSING",
                "来源工作目录已不存在",
                "恢复来源记录中的原工作目录后，用同一计划或指派 ID 重试。",
                true,
            )
        } else {
            e.into()
        }
    })?;
    ensure!(cwd.is_dir(), "来源 cwd 不是目录");
    let root = PathBuf::from(git(&cwd, &["rev-parse", "--show-toplevel"])?).canonicalize()?;
    let git_dir = PathBuf::from(git(&cwd, &["rev-parse", "--absolute-git-dir"])?).canonicalize()?;
    let head = git(&cwd, &["rev-parse", "HEAD"])?;
    Ok(Workspace {
        cwd,
        root,
        git_dir,
        head,
    })
}
fn notes(path: &Path, out: &mut Vec<PathBuf>) -> Result<()> {
    let meta = fs::symlink_metadata(path)?;
    ensure!(
        !meta.file_type().is_symlink(),
        "笔记不能是符号链接 {}",
        path.display()
    );
    if meta.is_dir() {
        ensure!(out.len() < 128, "笔记数量超过 128");
        for entry in fs::read_dir(path)? {
            let entry = entry?;
            if entry.file_type()?.is_dir()
                && matches!(
                    entry.file_name().to_str(),
                    Some(".git" | "node_modules" | "target")
                )
            {
                continue;
            }
            notes(&entry.path(), out)?;
        }
    } else if path.extension().is_some_and(|v| v == "md") {
        out.push(path.canonicalize()?);
        ensure!(out.len() <= 128, "笔记数量超过 128");
    } else {
        ensure!(
            !out.is_empty() || path.extension().is_some(),
            "请选择 Markdown 笔记"
        );
    }
    Ok(())
}
fn read_notes(db: &Connection, root: &Path) -> Result<Vec<Note>> {
    let mut files = Vec::new();
    let mut stmt = db.prepare("SELECT path FROM bindings WHERE project=? ORDER BY path")?;
    for path in stmt.query_map([root.to_string_lossy().as_ref()], |r| r.get::<_, String>(0))? {
        notes(Path::new(&path?), &mut files)?;
    }
    files.sort();
    files.dedup();
    let mut total = 0;
    files
        .into_iter()
        .map(|path| {
            let text = fs::read_to_string(&path)?;
            total += text.len();
            ensure!(
                total <= 32768,
                "绑定笔记超过首版 32 KiB 上限，请缩小绑定目录"
            );
            Ok(Note {
                digest: digest(text.as_bytes()),
                path,
                text,
            })
        })
        .collect()
}
fn load_plan(db: &Connection, id: &str) -> Result<(Plan, String)> {
    let (body, status): (String, String) = db
        .query_row("SELECT body,status FROM plans WHERE id=?", [id], |r| {
            Ok((r.get(0)?, r.get(1)?))
        })
        .context("找不到交接计划")?;
    Ok((serde_json::from_str(&body)?, status))
}
pub(crate) fn plan_view(db: &Connection, id: &str) -> Result<Value> {
    let (plan, status) = load_plan(db, id)?;
    Ok(view(&plan, &status))
}
fn view(plan: &Plan, status: &str) -> Value {
    let next = crate::agent::next_action(
        &plan.target_agent,
        &plan.target_id,
        &plan.id,
        &plan.workspace.cwd,
    );
    json!({"id":plan.id,"task_id":plan.task_id,"parent_plan_id":plan.parent_plan_id,
        "version_id":plan.version_id,"status":status,"source_agent":plan.source_agent,"source_id":plan.source_id,
        "source_ref":plan.source,"source_digest":plan.source_digest,"workspace":plan.workspace,
        "target_agent":plan.target_agent,"target_id":plan.target_id,"target_path":plan.target,
        "memory":plan.memory.iter().map(|n| json!({"path":n.path,"digest":n.digest})).collect::<Vec<_>>(),
        "losses":plan.losses,"next_action":next})
}
fn create_plan(
    cli: &Cli,
    db: &mut Connection,
    state: &Path,
    source: &str,
    source_agent: &str,
    target_agent: &str,
) -> Result<Value> {
    crate::agent::check_target(cli, target_agent)?;
    let version = capture(cli, db, source, source_agent)?;

    let (plan, mut preparation) =
        prepare_version(cli, db, state, &version.id, target_agent, true, None)?;
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    preparation.insert(&tx)?;
    preparation.preserve();
    tx.commit()?;
    preparation.finish();
    Ok(plan)
}
pub(crate) struct Preparation {
    path: PathBuf,
    preserve_directory: bool,
    plan: Option<Plan>,
}
impl Preparation {
    pub(crate) fn insert(&self, db: &Connection) -> Result<()> {
        let plan = self.plan.as_ref().context("准备产物缺少计划")?;
        db.execute(
            "INSERT INTO plans(id,body,status) VALUES(?,?,'previewed')",
            params![plan.id, serde_json::to_string(plan)?],
        )?;
        Ok(())
    }
    pub(crate) fn preserve(&mut self) {
        self.preserve_directory = true;
    }
    pub(crate) fn finish(&mut self) {
        self.preserve();
        // The durable commit-intent receipt remains even if the database is restored.
        let _ = fs::remove_file(self.path.join(".handoff-building"));
    }
}
impl Drop for Preparation {
    fn drop(&mut self) {
        if !self.preserve_directory {
            let _ = fs::remove_dir_all(&self.path);
        }
    }
}
pub(crate) fn prepare_version(
    cli: &Cli,
    db: &Connection,
    state: &Path,
    version_id: &str,
    target_agent: &str,
    verify_source: bool,
    instructions: Option<&str>,
) -> Result<(Value, Preparation)> {
    let version = load_version(db, version_id)?;
    db.execute("DELETE FROM archives WHERE task_id=?", [&version.task_id])?;
    let mut common: Transcript = Transcript::new(version.meta, version.messages);
    let ws = version.workspace;
    let memory = version.memory;
    let id = uuid::Uuid::new_v4().to_string();
    let target_id = uuid::Uuid::new_v4().to_string();
    let folder = state.join(&id);
    private_dir(&folder)?;
    let mut cleanup = Preparation {
        path: folder.clone(),
        preserve_directory: false,
        plan: None,
    };
    let identity: String =
        db.query_row("SELECT value FROM metadata WHERE key='state_id'", [], |r| {
            r.get(0)
        })?;
    write_new(&folder.join(".handoff-building"), identity.as_bytes())?;
    let mut losses = vec!["厂商私有运行状态、原系统指令和工具权限需由目标客户端重新建立".into()];
    if common.body.iter().flat_map(|m| &m.content).any(|b| {
        !matches!(
            b,
            Block::Text { .. } | Block::ToolUse { .. } | Block::ToolResult { .. }
        )
    }) {
        losses.push("目标省略源推理块及私有签名；来源文本快照可回读。图片与附件需单独核对".into());
    }
    write_new(
        &folder.join("transcript.txt"),
        txcript::text::to_text(&common).as_bytes(),
    )?;
    let mut briefing = format!(
        "接手来源 {} 会话 {}。原工作目录 {}。以下笔记正文是本版本已保存的项目资料，来源路径仅用于追溯。请直接依据正文回忆约定和标记，历史工具调用作为记录。\n",
        version.source_agent,
        version.source_id,
        ws.cwd.display()
    );
    for note in &memory {
        briefing.push_str(&format!(
            "\n笔记来源 {}\n{}\n",
            note.path.display(),
            note.text
        ));
    }
    if let Some(instructions) = instructions {
        briefing.push_str(&format!("\n本次指派的后续要求\n{instructions}\n"));
    }
    common.body.push(Message {
        role: Role::User,
        content: vec![Block::Text { text: briefing }],
        timestamp: chrono::Utc::now(),
        model: None,
        stop_reason: None,
        usage: None,
    });
    common.meta.id = target_id.clone();
    common.meta.cwd = Some(ws.cwd.to_string_lossy().into());
    common.meta.title = Some(format!("Handoff {}", &id[..8]));
    // Vendor reasoning signatures are not valid replay state in another harness.
    for message in &mut common.body {
        message
            .content
            .retain(|block| !matches!(block, Block::Thinking { .. }));
    }
    common.body.retain(|message| !message.content.is_empty());
    let staging = folder.join("staging");
    let (bytes, target, delivered) =
        crate::agent::prepare(cli, target_agent, &mut common, &staging, &id, &target_id)?;
    let prepared = folder.join("native.jsonl");
    write_new(&prepared, &bytes)?;
    let (history, history_digest) = if crate::agent::builtin_target(target_agent) {
        let bytes = serde_json::to_vec(&HistoryManifest::from_messages(&delivered.body)?)?;
        write_new(&folder.join("history.json"), &bytes)?;
        (Some(folder.join("history.json")), Some(digest(&bytes)))
    } else {
        (None, None)
    };
    let plan = Plan {
        id: id.clone(),
        task_id: version.task_id,
        parent_plan_id: version.parent_plan_id,
        version_id: Some(version.id),
        verify_source,
        source_agent: version.source_agent,
        target_agent: target_agent.into(),
        source_id: version.source_id,
        source: version.source,
        source_digest: version.source_digest,
        workspace: ws,
        memory,
        target_id,
        target,
        prepared,
        prepared_digest: digest(&bytes),
        transcript: folder.join("transcript.txt"),
        history,
        history_digest,
        losses,
    };
    // Persist the commit boundary before inserting into SQLite. Absence from an
    // older database cannot distinguish rollback from a previously committed plan.
    write_new(&folder.join(".handoff-commit-intent"), identity.as_bytes())?;
    fs::File::open(&folder)?.sync_all()?;
    fs::File::open(state)?.sync_all()?;
    let result = view(&plan, "previewed");
    cleanup.plan = Some(plan);
    Ok((result, cleanup))
}
fn verify_published_target(cli: &Cli, plan: &Plan) -> Result<()> {
    let target = crate::agent::validate_target(
        cli,
        &plan.target_agent,
        &plan.target_id,
        &plan.target,
        &plan.workspace.cwd,
    )?;
    if crate::agent::builtin_target(&plan.target_agent) {
        verify_history(cli, plan, &target.body)?;
    } else {
        let prepared = fs::read(&plan.prepared)?;
        ensure!(
            digest(&prepared) == plan.prepared_digest,
            crate::error::plan_corrupt()
        );
        ensure!(
            crate::protocol::continued(&plan.target_id, &prepared, &fs::read(&plan.target)?),
            crate::error::target_invalid()
        );
    }
    Ok(())
}

pub(crate) fn apply(cli: &Cli, db: &mut Connection, id: &str) -> Result<Value> {
    let (plan, status) = load_plan(db, id)?;
    if !crate::agent::builtin_target(&plan.target_agent) {
        ensure!(
            crate::protocol::target(cli, &plan.target_agent, &plan.target_id)? == plan.target,
            crate::error::target_invalid()
        );
    }
    let ws = workspace(&plan.workspace.cwd)?;
    ensure!(
        ws.git_dir == plan.workspace.git_dir && ws.root == plan.workspace.root,
        "工作树身份已变化"
    );
    db.execute("DELETE FROM archives WHERE task_id=?", [&plan.task_id])?;
    if status != "previewed" {
        let metadata = fs::metadata(&plan.target).map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                crate::error::fault("TARGET_MISSING", "已发布的目标会话已不存在", "从备份恢复该目标会话后重试；需要重新交接时从保存版本创建新计划，核对丢失的后续工作。", false)
            } else { e.into() }
        })?;
        ensure!(
            metadata.is_file(),
            crate::error::fault(
                "TARGET_INVALID",
                "目标会话路径不是文件",
                "恢复原目标会话文件后，用同一 ID 重试。",
                true
            )
        );
    }
    let builtin = crate::agent::builtin_target(&plan.target_agent);
    // Publication may survive a crash before SQLite records it. Validate a
    // client-edited file outside the write transaction, then recheck its bytes.
    let existing = if status == "previewed" && builtin && plan.target.is_file() {
        Some(digest(&fs::read(&plan.target)?))
    } else {
        None
    };
    if status != "previewed" || existing.is_some() {
        verify_published_target(cli, &plan).map_err(|error| {
            if status == "previewed" && crate::error::is_target_invalid(&error) {
                anyhow::anyhow!("目标 ID 已存在且内容不同")
            } else {
                error
            }
        })?;
    }
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    if status == "previewed" {
        if plan.verify_source {
            ensure!(
                digest(&fs::read(&plan.source)?) == plan.source_digest,
                "来源已变化，请重新预览"
            );
        }
        ensure!(
            ws.head == plan.workspace.head,
            "工作树身份或 HEAD 已变化，请重新预览"
        );
        if plan.verify_source {
            ensure!(
                serde_json::to_string(&read_notes(&tx, &ws.root)?)?
                    == serde_json::to_string(&plan.memory)?,
                "绑定笔记已变化，请重新预览"
            );
        }
        let bytes = fs::read(&plan.prepared)?;
        ensure!(
            digest(&bytes) == plan.prepared_digest,
            crate::error::plan_corrupt()
        );
        private_dir(plan.target.parent().context("无目标父目录")?)?;
        if plan.target.exists() {
            let current = fs::read(&plan.target)?;
            let matches = if builtin {
                current == bytes || existing.as_ref() == Some(&digest(&current))
            } else {
                current == bytes || crate::protocol::continued(&plan.target_id, &bytes, &current)
            };
            ensure!(matches, "目标 ID 已存在且内容不同");
        } else {
            // Publish a complete file without overwriting a native session. A crash
            // before publication leaves only this plan's disposable temporary file.
            let temp = plan.target.with_extension("handoff-tmp");
            if temp.exists() {
                fs::remove_file(&temp)?;
            }
            write_new(&temp, &bytes)?;
            fs::hard_link(&temp, &plan.target).context("发布目标会话失败")?;
            fs::File::open(plan.target.parent().context("无目标目录")?)?.sync_all()?;
            fs::remove_file(temp)?;
        }
        tx.execute(
            "UPDATE plans SET status='waiting_for_open' WHERE id=?",
            [id],
        )?;
    }
    tx.commit()?;

    let result = view(
        &plan,
        if status == "previewed" {
            "waiting_for_open"
        } else {
            &status
        },
    );
    Ok(result)
}
// The lock remains at a stable sibling path while restore swaps the state tree.
pub(crate) fn operation_lock(
    state: &Path,
    seconds: u64,
    cancelled: Option<&std::sync::atomic::AtomicBool>,
) -> Result<Vec<fs::File>> {
    let path = crate::maintenance::control_path(state, "lock")?;
    let file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(path)?;
    let started = std::time::Instant::now();
    let mut locks = vec![file];
    // Respect the legacy lock when present. All current binaries also hold the
    // stable sibling lock, including during a state-directory replacement.
    let legacy = state.join(".handoff-operation.lock");
    if legacy.try_exists()? {
        locks.push(fs::OpenOptions::new().read(true).write(true).open(legacy)?);
    }
    for file in &locks {
        loop {
            if cancelled.is_some_and(|flag| flag.load(std::sync::atomic::Ordering::Relaxed)) {
                return Err(crate::error::fault(
                    "REQUEST_CANCELLED",
                    "客户端已断开",
                    "重新连接后核对原指派或计划状态。",
                    true,
                ));
            }
            match file.try_lock() {
                Ok(()) => break,
                Err(std::fs::TryLockError::WouldBlock) => {}
                Err(std::fs::TryLockError::Error(error)) => return Err(error.into()),
            }
            if started.elapsed() >= std::time::Duration::from_secs(seconds) {
                return Err(crate::error::fault(
                    "STATE_BUSY",
                    "中转站写操作等待超时",
                    "稍后用原计划或指派 ID 重试。",
                    true,
                ));
            }
            std::thread::sleep(std::time::Duration::from_millis(25));
        }
    }
    Ok(locks)
}

fn cleanup_orphans(db: &mut Connection, state: &Path) -> Result<()> {
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    for entry in fs::read_dir(state)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if uuid::Uuid::parse_str(&name).is_err() || !entry.file_type()?.is_dir() {
            continue;
        }
        // Legacy preparation markers are ambiguous and must be retained.
        // New building markers precede every possible database commit.
        let marker = entry.path().join(".handoff-building");
        if entry.path().join(".handoff-commit-intent").try_exists()? {
            continue;
        }
        let owner = match fs::read_to_string(&marker) {
            Ok(owner) => owner,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error.into()),
        };
        let identity: String =
            tx.query_row("SELECT value FROM metadata WHERE key='state_id'", [], |r| {
                r.get(0)
            })?;
        if owner != identity {
            continue;
        }
        let exists: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM plans WHERE id=?)",
            [&name],
            |r| r.get(0),
        )?;
        if !exists {
            fs::remove_dir_all(entry.path())?;
        }
    }
    tx.commit()?;
    Ok(())
}
pub(crate) fn run(mut cli: Cli) -> Result<Value> {
    let state = cli
        .state_dir
        .clone()
        .unwrap_or(home()?.join(".local/share/agent-handoff"));
    let read_only = matches!(
        &cli.command,
        Action::Sessions { .. }
            | Action::Agents
            | Action::Versions { .. }
            | Action::Inbox { .. }
            | Action::Status { .. }
            | Action::Read { .. }
            | Action::Memory {
                action: MemoryAction::List { .. }
            }
            | Action::Gc { execute: false, .. }
    );

    let state = if state.exists() {
        state.canonicalize()?
    } else {
        std::path::absolute(state)?
    };
    if let Action::Restore { path } = &cli.command {
        return crate::maintenance::restore(
            &state,
            path,
            cli.lock_timeout_seconds,
            cli.disconnected.as_deref(),
        );
    }
    let write_lock = if !read_only {
        private_dir(state.parent().context("无状态父目录")?)?;
        let lock = operation_lock(
            &state,
            cli.lock_timeout_seconds,
            cli.disconnected.as_deref(),
        )?;
        crate::maintenance::recover_restore(&state)?;
        private_dir(&state)?;
        Some(lock)
    } else {
        None
    };
    let database = state.join("handoff.sqlite3");
    let initialized = state.join(".handoff-state");
    let database_exists = database.try_exists()?;
    if !database_exists
        && (initialized.try_exists()?
            || matches!(
                &cli.command,
                Action::Versions { .. }
                    | Action::Status { .. }
                    | Action::Read { .. }
                    | Action::Inbox { .. }
                    | Action::Apply { .. }
                    | Action::Claim { .. }
                    | Action::Prepare { .. }
            ))
    {
        return Err(crate::error::fault(
            "STATE_MISSING",
            "中转站状态数据库不存在",
            "恢复原 handoff.sqlite3 及配套数据库文件后，用原计划或指派 ID 重试。",
            true,
        ));
    }
    let mut flags = if read_only {
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY
    } else {
        rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE
    };
    if !database_exists {
        flags |= rusqlite::OpenFlags::SQLITE_OPEN_CREATE;
    }
    let mut db = if read_only && !database_exists {
        Connection::open_in_memory()?
    } else {
        if read_only {
            crate::maintenance::read_snapshot(&database)?
        } else {
            Connection::open_with_flags(database, flags)?
        }
    };
    db.busy_timeout(std::time::Duration::from_secs(10))?;
    if !read_only || !database_exists {
        db.execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS protocol_agents(id TEXT PRIMARY KEY, role TEXT NOT NULL); CREATE TABLE IF NOT EXISTS assignments(id TEXT PRIMARY KEY,body TEXT NOT NULL,plan_id TEXT REFERENCES plans(id)); CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,body TEXT NOT NULL); CREATE TABLE IF NOT EXISTS bindings(project TEXT NOT NULL,path TEXT NOT NULL,PRIMARY KEY(project,path)); CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,body TEXT NOT NULL,status TEXT NOT NULL);")?;
        {
            let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
            tx.execute_batch(
                "CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
            )?;
            tx.execute(
                "INSERT OR IGNORE INTO metadata(key,value) VALUES('state_id',?)",
                [uuid::Uuid::new_v4().to_string()],
            )?;
            if !read_only && !initialized.exists() {
                write_new(&initialized, b"agent-handoff state v1")?;
            }
            tx.commit()?;
        }
    }
    if !read_only {
        crate::maintenance::migrate(&mut db)?;
        crate::maintenance::recover_gc(&db, &state)?;
        cleanup_orphans(&mut db, &state)?;
    }
    cli.state_dir = Some(state.clone());
    cli.registered = crate::protocol::registry(&db)?;
    let mut result = (|| match &cli.command {
        Action::Backup { path } => crate::maintenance::backup(&mut db, &state, path),
        Action::Restore { .. } => unreachable!(),
        Action::Archive { task_id } => crate::maintenance::archive(&mut db, &state, task_id),
        Action::Gc {
            older_than,
            execute,
        } => crate::maintenance::gc(&mut db, &state, older_than, *execute),
        Action::Memory {
            action: MemoryAction::List { project },
        } => {
            let ws = workspace(project)?;
            let mut stmt = db.prepare("SELECT path FROM bindings WHERE project=? ORDER BY path")?;
            let paths = stmt
                .query_map([ws.root.to_string_lossy().as_ref()], |r| {
                    r.get::<_, String>(0)
                })?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            Ok(
                json!({"bindings":paths.into_iter().map(|path|json!({"available":Path::new(&path).is_file() || Path::new(&path).is_dir(),"path":path})).collect::<Vec<_>>()}),
            )
        }
        Action::Memory {
            action: MemoryAction::Unbind { project, path },
        } => {
            let ws = workspace(project)?;
            let path = if path.exists() {
                path.canonicalize()?
            } else {
                crate::maintenance::absolute(path)?
            };
            let removed = db.execute(
                "DELETE FROM bindings WHERE project=? AND path=?",
                params![ws.root.to_string_lossy(), path.to_string_lossy()],
            )?;
            Ok(json!({"removed":removed,"path":path}))
        }
        Action::Agents => Ok(crate::protocol::list(&cli)),
        Action::RegisterAgent {
            id,
            role,
            protocol_version,
        } => crate::protocol::register(&cli, &mut db, id, role, *protocol_version),
        Action::Mcp | Action::AdapterSupervisor { .. } => {
            anyhow::bail!("内部入口不能作为业务操作执行")
        }
        Action::Sessions {
            project,
            query,
            limit,
            offset,
            agent,
        } => {
            let project = project.as_ref().map(|p| p.canonicalize()).transpose()?;
            let (mut items, mut warnings) = discover(&cli, agent)?;
            items.sort_by(|a, b| {
                b.meta
                    .timestamp
                    .cmp(&a.meta.timestamp)
                    .then_with(|| a.reference.cmp(&b.reference))
            });
            ensure!(*limit > 0, "limit 必须大于零");
            let mut sessions = Vec::new();
            for item in items {
                if project.as_ref().is_some_and(|p| {
                    item.meta
                        .cwd
                        .as_ref()
                        .is_none_or(|c| !Path::new(c).starts_with(p))
                }) {
                    continue;
                }
                if let Some(q) = query {
                    let metadata = format!(
                        "{} {}",
                        item.meta.id,
                        item.meta.title.as_deref().unwrap_or("")
                    );
                    if !metadata.contains(q) {
                        let common = match fs::read(&item.reference)
                            .map_err(anyhow::Error::from)
                            .and_then(|bytes| load_common(&cli, agent, &item.meta.id, &bytes))
                        {
                            Ok(common) => common,
                            Err(error) => {
                                warnings.push(json!({"source_ref":item.reference,"issue":crate::error::response(&error)}));
                                continue;
                            }
                        };
                        if !txcript::text::to_text(&common).contains(q) {
                            continue;
                        }
                    }
                }
                sessions.push(json!({"agent":agent,"id":item.meta.id,"source_ref":item.reference,"cwd":item.meta.cwd,"title":item.meta.title,"timestamp":item.meta.timestamp}));
            }
            let total = sessions.len();
            let end = offset.saturating_add(*limit).min(total);
            let page = sessions.get(*offset..end).unwrap_or(&[]);
            Ok(
                json!({"sessions":page,"total":total,"partial":!warnings.is_empty(),"warnings":warnings,"next_cursor":if end<total {Some(end)} else {None}}),
            )
        }
        Action::Memory {
            action: MemoryAction::Bind { project, path },
        } => {
            let ws = workspace(project)?;
            let path = path.canonicalize()?;
            let mut files = Vec::new();
            notes(&path, &mut files)?;
            ensure!(!files.is_empty(), "没有 Markdown 笔记");
            db.execute(
                "INSERT OR IGNORE INTO bindings(project,path) VALUES(?,?)",
                params![ws.root.to_string_lossy(), path.to_string_lossy()],
            )?;
            Ok(json!({"project":ws.root,"path":path}))
        }
        Action::Preview {
            source, from, to, ..
        } => create_plan(&cli, &mut db, &state, source, from, to),
        Action::Capture { source, from } => {
            Ok(version_view(&capture(&cli, &mut db, source, from)?))
        }
        Action::Prepare { version, to } => {
            let (plan, mut preparation) =
                prepare_version(&cli, &db, &state, version, to, false, None)?;
            let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
            preparation.insert(&tx)?;
            preparation.preserve();
            tx.commit()?;
            preparation.finish();
            Ok(plan)
        }
        Action::Versions {
            task_id,
            limit,
            offset,
            cursor,
        } => {
            let (rows, total, next_cursor) = crate::maintenance::page(
                &db,
                "versions",
                "task_id",
                task_id.as_deref(),
                *limit,
                *offset,
                *cursor,
            )?;
            let versions = rows
                .into_iter()
                .map(|body| serde_json::from_str::<Version>(&body).map(|v| version_view(&v)))
                .collect::<serde_json::Result<Vec<_>>>()?;
            Ok(json!({"versions":versions,"total":total,"next_cursor":next_cursor}))
        }
        Action::Assign {
            version,
            to,
            instructions,
        } => crate::assignment::assign(&cli, &db, version, to, instructions),
        Action::Inbox {
            recipient,
            limit,
            offset,
            cursor,
        } => crate::assignment::inbox(&cli, &db, recipient, *limit, *offset, *cursor),
        Action::Claim {
            assignment,
            recipient,
            open: _,
        } => crate::assignment::claim(&cli, &mut db, &state, assignment, recipient),
        Action::Apply { plan, open: _ } => apply(&cli, &mut db, plan),
        Action::Status { id } => {
            let (p, s) = load_plan(&db, id)?;
            Ok(view(&p, &s))
        }
        Action::Read {
            id,
            section,
            cursor,
        } => {
            let is_version: bool = db.query_row(
                "SELECT EXISTS(SELECT 1 FROM versions WHERE id=?)",
                [id],
                |r| r.get(0),
            )?;
            let text = if is_version {
                let v = load_version(&db, id)?;
                if section == "memory" {
                    serde_json::to_string_pretty(&v.memory)?
                } else {
                    txcript::text::to_text(&Transcript::<txcript::Common>::new(v.meta, v.messages))
                }
            } else {
                let (p, _) = load_plan(&db, id)?;
                if section == "memory" {
                    serde_json::to_string_pretty(&p.memory)?
                } else {
                    fs::read_to_string(p.transcript)?
                }
            };
            let chars: Vec<char> = text.chars().collect();
            ensure!(*cursor <= chars.len(), "无效游标");
            let end = cursor.saturating_add(16000).min(chars.len());
            Ok(
                json!({"text":chars[*cursor..end].iter().collect::<String>(),"next_cursor":if end<chars.len(){Some(end)}else{None}}),
            )
        }
    })();

    let requested = matches!(
        cli.command,
        Action::Apply { open: true, .. } | Action::Claim { open: true, .. }
    );
    let identity = if requested && result.is_ok() {
        Some(
            db.query_row("SELECT value FROM metadata WHERE key='state_id'", [], |r| {
                r.get::<_, String>(0)
            })?,
        )
    } else {
        None
    };
    // No SQLite handle may outlive the unlocked launcher operation: restore can
    // replace the active database while the desktop client is starting.
    drop(db);
    drop(write_lock);
    if requested {
        if let Ok(value) = &mut result {
            let plan = if value.get("plan").is_some() {
                &mut value["plan"]
            } else {
                value
            };
            let id = plan["id"].as_str().context("缺少计划 ID")?.to_string();
            let target = plan["target_agent"].as_str().context("缺少目标")?;
            let target_id = plan["target_id"].as_str().context("缺少目标 ID")?;
            let cwd = Path::new(plan["workspace"]["cwd"].as_str().context("缺少目录")?);
            let target_id = target_id.to_string();
            let opened = crate::agent::open(target, &target_id, cwd);
            let _lock = operation_lock(
                &state,
                cli.lock_timeout_seconds,
                cli.disconnected.as_deref(),
            )?;
            crate::maintenance::recover_restore(&state)?;
            let mut current = Connection::open_with_flags(
                state.join("handoff.sqlite3"),
                rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
            )?;
            current.busy_timeout(std::time::Duration::from_secs(10))?;
            let active_identity: String =
                current.query_row("SELECT value FROM metadata WHERE key='state_id'", [], |r| {
                    r.get(0)
                })?;
            ensure!(
                Some(active_identity) == identity,
                crate::error::fault(
                    "STATE_CHANGED",
                    "启动期间状态库身份发生变化",
                    "核对当前状态和原计划后重试。",
                    true
                )
            );
            let (active, status) = load_plan(&current, &id)?;
            ensure!(
                active.target_id == target_id
                    && active.target_agent == target
                    && active.workspace.cwd == cwd,
                crate::error::fault(
                    "PLAN_CHANGED",
                    "启动期间计划身份发生变化",
                    "核对当前计划及目标会话后重试。",
                    true
                )
            );
            if let Action::Claim { assignment, .. } = &cli.command {
                let bound: Option<String> = current
                    .query_row(
                        "SELECT plan_id FROM assignments WHERE id=?",
                        [assignment],
                        |r| r.get(0),
                    )
                    .context("找不到指派")?;
                ensure!(
                    bound.as_deref() == Some(id.as_str()),
                    crate::error::fault(
                        "PLAN_CHANGED",
                        "启动期间指派绑定发生变化",
                        "核对原指派后重试。",
                        true
                    )
                );
            }
            match opened {
                Ok(()) => {
                    verify_published_target(&cli, &active)?;
                    let tx = current
                        .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
                    tx.execute("UPDATE plans SET status='open_requested' WHERE id=?", [&id])?;
                    tx.commit()?;
                    *plan = view(&active, "open_requested");
                }
                Err(error) => {
                    *plan = view(&active, &status);
                    let issue = crate::error::response(&error);
                    plan["open_error"] = issue["error"].clone();
                    plan["open_issue"] = issue;
                }
            }
        }
        if let Ok(value) = &mut result {
            if let Some(status) = value.get("plan").and_then(|p| p.get("status")).cloned() {
                value["status"] = status;
            }
        }
    }
    result
}
