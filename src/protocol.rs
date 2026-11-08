// Local agent protocol v1. Each registration owns one flat session directory.
use crate::{app::Cli, error::fault};
use anyhow::{Result, ensure};
use rusqlite::Connection;
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};
use txcript::{
    Discovered, Transcript,
    common::{Message, Meta},
};

pub(crate) type Registry = BTreeMap<String, String>;

pub(crate) fn registry(db: &Connection) -> Result<Registry> {
    let mut stmt = db.prepare("SELECT id, role FROM protocol_agents")?;
    Ok(stmt
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?)
}

fn invalid() -> anyhow::Error {
    fault(
        "INVALID_REGISTRATION",
        "接入声明无效",
        "使用未被保留的 1–64 位小写 ASCII 标识和 source、target 或 both 角色。",
        false,
    )
}

fn descriptor(id: &str, role: &str, dir: &Path) -> Value {
    json!({"id":id,"builtin":false,"source_agent":(role != "target").then_some(id),
           "target_agent":(role != "source").then_some(id),"protocol_version":1,"sessions_dir":dir})
}

pub(crate) fn list(cli: &Cli) -> Value {
    let mut agents = vec![];
    for (id, target) in [
        ("codex", "codex"),
        ("claude", "claude-desktop"),
        ("deepseek", "deepseek"),
    ] {
        agents.push(json!({"id":id,"source_agent":id,"target_agent":target,"builtin":true}));
    }
    for (id, role) in &cli.registered {
        agents.push(descriptor(id, role, &directory(cli, id)));
    }
    json!({"agents":agents,"protocol_versions":[1],
           "protocol_v1":{
               "filename":"<meta.id>.json",
               "session_example":{"protocol_version":1,
                   "meta":{"id":"sample","timestamp":"2026-01-01T00:00:00Z","cwd":"<absolute Git worktree path>"},
                   "messages":[{"timestamp":"2026-01-01T00:00:00Z","role":"user","content":[{"type":"text","text":"Resume here"}]}]}}})
}

pub(crate) fn register(
    cli: &Cli,
    db: &mut Connection,
    id: &str,
    role: &str,
    version: u32,
) -> Result<Value> {
    ensure!(
        version == 1,
        fault(
            "PROTOCOL_UNSUPPORTED",
            "接入协议版本不受支持",
            "调用 list_agents 获取支持的版本。",
            false
        )
    );
    ensure!(
        !id.is_empty()
            && id.len() <= 64
            && id.as_bytes()[0].is_ascii_lowercase()
            && id
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
            && !matches!(id, "codex" | "claude" | "claude-desktop" | "deepseek")
            && matches!(role, "source" | "target" | "both"),
        invalid()
    );
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    tx.execute(
        "INSERT OR IGNORE INTO protocol_agents(id,role) VALUES(?,?)",
        [id, role],
    )?;
    let stored: String =
        tx.query_row("SELECT role FROM protocol_agents WHERE id=?", [id], |r| {
            r.get(0)
        })?;
    ensure!(
        stored == role,
        fault(
            "REGISTRATION_CONFLICT",
            "标识已按不同能力注册",
            "核对原声明，或选择其他 agent 标识。",
            false
        )
    );
    let dir = directory(cli, id);
    let root = dir.parent().expect("agent directory has parent");
    for path in [root, dir.as_path()] {
        // Never follow a pre-existing link when establishing the exchange.
        match fs::symlink_metadata(path) {
            Ok(meta) => ensure!(meta.is_dir(), unsafe_path()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => crate::app::private_dir(path)?,
            Err(e) => return Err(e.into()),
        }
    }
    tx.commit()?;
    Ok(descriptor(id, role, &dir))
}

fn directory(cli: &Cli, id: &str) -> PathBuf {
    cli.state_dir
        .as_ref()
        .expect("initialized state")
        .join("agents")
        .join(id)
}

fn unsafe_path() -> anyhow::Error {
    fault(
        "PROTOCOL_PATH_INVALID",
        "协议路径不是预期的本地目录或普通文件",
        "恢复注册时分配的真实目录及普通会话文件后重试。",
        true,
    )
}

pub(crate) fn require(cli: &Cli, id: &str, role: &str) -> Result<()> {
    let stored = cli.registered.get(id).ok_or_else(|| {
        fault(
            "AGENT_UNKNOWN",
            "找不到已注册的 agent",
            "调用 list_agents 核对来源和目标标识，或先注册 agent。",
            false,
        )
    })?;
    ensure!(
        stored == "both" || stored == role,
        fault(
            "CAPABILITY_UNSUPPORTED",
            "该 agent 未声明此方向的能力",
            "调用 list_agents 选择具有相应能力的 agent。",
            false
        )
    );
    Ok(())
}

pub(crate) fn target(cli: &Cli, id: &str, session: &str) -> Result<PathBuf> {
    require(cli, id, "target")?;
    let dir = checked_dir(cli, id)?;
    let path = dir.join(format!("{session}.json"));
    match fs::symlink_metadata(&path) {
        Ok(meta) => ensure!(meta.is_file(), unsafe_path()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(e.into()),
    }
    Ok(path)
}

fn checked_dir(cli: &Cli, id: &str) -> Result<PathBuf> {
    let dir = directory(cli, id);
    ensure!(dir.canonicalize()? == dir && dir.is_dir(), unsafe_path());
    Ok(dir)
}

#[derive(Deserialize)]
struct Session {
    protocol_version: u32,
    meta: Meta,
    messages: Vec<Message>,
}

pub(crate) fn decode(id: &str, bytes: &[u8]) -> Result<Transcript> {
    let invalid = || {
        fault(
            "SOURCE_INVALID",
            "协议会话内容或身份无效",
            "核对协议版本、文件名、会话 ID、消息格式及绝对工作目录。",
            false,
        )
    };
    let s: Session = serde_json::from_slice(bytes).map_err(|_| invalid())?;
    ensure!(
        s.protocol_version == 1 && s.meta.id == id && !id.is_empty() && !s.messages.is_empty(),
        invalid()
    );
    if let Some(cwd) = &s.meta.cwd {
        ensure!(Path::new(cwd).is_absolute(), invalid());
    }
    Ok(Transcript::new(s.meta, s.messages))
}

// A client may atomically reserialize and extend the published JSON before a
// crash-recovered plan has recorded publication in SQLite.
pub(crate) fn continued(id: &str, prepared: &[u8], current: &[u8]) -> bool {
    let (Ok(original), Ok(latest)) = (decode(id, prepared), decode(id, current)) else {
        return false;
    };
    original.meta.cwd == latest.meta.cwd && latest.body.starts_with(&original.body)
}

pub(crate) fn encode(common: &Transcript) -> Result<Vec<u8>> {
    Ok(serde_json::to_vec(
        &json!({"protocol_version":1,"meta":common.meta,"messages":common.body}),
    )?)
}

pub(crate) fn discover(cli: &Cli, agent: &str) -> Result<(Vec<Discovered<PathBuf>>, Vec<Value>)> {
    require(cli, agent, "source")?;
    let mut items = vec![];
    let mut warnings = vec![];
    for entry in fs::read_dir(checked_dir(cli, agent)?)? {
        let entry = entry?;
        let path = entry.path();
        if path.extension().and_then(|s| s.to_str()) != Some("json") {
            continue;
        }
        let id = path.file_stem().and_then(|s| s.to_str()).unwrap_or("");
        let parsed = (|| {
            ensure!(entry.file_type()?.is_file(), unsafe_path());
            Ok(decode(id, &fs::read(&path)?)?.meta)
        })();
        match parsed {
            Ok(meta) => items.push(Discovered {
                meta,
                reference: path,
            }),
            Err(e) => warnings.push(
                json!({"source_ref":path,"session_id":id,"issue":crate::error::response(&e)}),
            ),
        }
    }
    Ok((items, warnings))
}
