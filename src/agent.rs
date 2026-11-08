use crate::app::Cli;
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{
    collections::HashSet,
    path::{Path, PathBuf},
    process::Command,
};
use txcript::{
    Codec, Store, TextCodec,
    harness::{
        claude_code::{ClaudeCode, ClaudeStore},
        codex::{Codex, CodexStore},
    },
};

fn claude(cli: &Cli) -> Result<ClaudeStore> {
    Ok(match &cli.claude_root {
        Some(root) => ClaudeStore::new(std::env::current_dir()?.join(root)),
        None => ClaudeStore::default_root().context("找不到 Claude 会话目录")?,
    })
}
fn deepseek(cli: &Cli) -> Result<crate::deepseek::DeepSeek> {
    crate::deepseek::DeepSeek::new(
        cli.dsh_root.as_deref(),
        cli.dsh_package.as_deref(),
        cli.adapter_timeout_seconds,
    )
    .map_err(|_| {
        crate::error::fault(
            "ADAPTER_UNAVAILABLE",
            "DeepSeek SDK 不可用",
            "安装兼容的 Harness，或用 --dsh-package 指向有效包目录后重试。",
            true,
        )
    })
}
pub(crate) fn discover(
    cli: &Cli,
    agent: &str,
) -> Result<(Vec<txcript::Discovered<PathBuf>>, Vec<Value>)> {
    let (items, root) = match agent {
        "codex" => {
            let store = codex(cli)?;
            (store.discover()?, store.sessions_dir)
        }
        "claude" => {
            let store = claude(cli)?;
            (store.discover()?, store.root)
        }
        "deepseek" => {
            let store = deepseek(cli)?;
            (store.discover()?, store.root)
        }
        _ => return crate::protocol::discover(cli, agent),
    };
    let mut warnings = Vec::new();
    let references = items.iter().map(|item| item.reference.clone()).collect();
    scan_candidates(&root, agent, &references, &mut warnings)?;
    Ok((items, warnings))
}
fn scan_candidates(
    dir: &Path,
    agent: &str,
    references: &HashSet<PathBuf>,
    warnings: &mut Vec<Value>,
) -> Result<()> {
    if !dir.exists() {
        return Ok(());
    }
    let entries = match std::fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(error) => {
            warnings.push(json!({"source_ref":dir,"issue":crate::error::response(&error.into())}));
            return Ok(());
        }
    };
    for entry in entries {
        let entry = entry?;
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        if entry.file_type()?.is_dir() {
            if agent != "claude" || !matches!(name.as_str(), "subagents" | "tool-results") {
                scan_candidates(&path, agent, references, warnings)?;
            }
        } else {
            let candidate = match agent {
                "codex" => {
                    name.starts_with("rollout-") && name.to_ascii_lowercase().ends_with(".jsonl")
                }
                "claude" => name.ends_with(".jsonl"),
                _ => {
                    name.ends_with(".jsonl")
                        || name.ends_with(".jsonl.zstd")
                        || name.ends_with(".jsonl.zst")
                }
            };
            if candidate && !references.contains(&path) {
                warnings.push(json!({"source_ref":path,"issue":crate::error::response(&crate::error::fault(
                    "SOURCE_INVALID", "来源会话头无法读取", "修复该来源记录，或选择已保存的版本。", false))}));
            }
        }
    }
    Ok(())
}
pub(crate) fn validate_target(
    cli: &Cli,
    agent: &str,
    id: &str,
    path: &Path,
    cwd: &Path,
) -> Result<txcript::Transcript<txcript::Common>> {
    // Access and adapter errors retain their retry instructions.
    let bytes = std::fs::read(path)?;
    ensure!(!bytes.is_empty(), crate::error::target_invalid());
    if agent == "deepseek" {
        let mut config = cli.clone();
        // Harness v4 stores root/project/session-id/session.v4.jsonl.zstd.
        config.dsh_root = path.ancestors().nth(3).map(Path::to_path_buf);
        let adapter = deepseek(&config)?;
        adapter.validate(id, cwd, path)?;
        let common = adapter.read(id)?;
        ensure!(
            common.meta.id == id
                && common.meta.cwd.as_deref() == cwd.to_str()
                && !common.body.is_empty(),
            crate::error::target_invalid()
        );
        return Ok(common);
    }
    let source_agent = if agent == "claude-desktop" {
        "claude"
    } else {
        agent
    };
    let common =
        load_common(cli, source_agent, id, &bytes).map_err(|_| crate::error::target_invalid())?;
    ensure!(
        common.meta.id == id
            && common.meta.cwd.as_deref() == cwd.to_str()
            && !common.body.is_empty(),
        crate::error::target_invalid()
    );
    Ok(common)
}

fn codex(cli: &Cli) -> Result<CodexStore> {
    Ok(match &cli.codex_root {
        Some(root) => CodexStore::new(std::env::current_dir()?.join(root)),
        None => CodexStore::default_root().context("找不到 Codex 会话目录")?,
    })
}
// Current Codex can emit a content-block array where txcript 0.14.4 expects text.
fn normalize_codex(text: &str) -> Result<String> {
    let mut out = String::new();
    for line in text.lines().filter(|line| !line.trim().is_empty()) {
        let mut row: Value = serde_json::from_str(line)?;
        if row["type"] == "response_item"
            && matches!(
                row["payload"]["type"].as_str(),
                Some("custom_tool_call_output" | "function_call_output")
            )
            && let Some(blocks) = row["payload"]["output"].as_array()
        {
            let pieces: Result<Vec<_>> = blocks
                .iter()
                .map(|block| {
                    block["text"]
                        .as_str()
                        .context("工具结果包含无法转换的非文本块")
                })
                .collect();
            row["payload"]["output"] = json!(pieces?.join("\n"));
        }
        out.push_str(&serde_json::to_string(&row)?);
        out.push('\n');
    }
    Ok(out)
}
pub(crate) fn load_common(
    cli: &Cli,
    agent: &str,
    id: &str,
    bytes: &[u8],
) -> Result<txcript::Transcript<txcript::Common>> {
    if !matches!(agent, "codex" | "claude" | "deepseek") {
        return crate::protocol::decode(id, bytes);
    }
    let common = if agent == "deepseek" {
        deepseek(cli)?.read(id)?
    } else {
        let text =
            String::from_utf8(bytes.to_vec()).context("来源必须是未压缩的 Codex/Claude JSONL")?;
        match agent {
            "codex" => Codex::to_common(&Codex::from_text(&normalize_codex(&text)?)?)?,
            _ => ClaudeCode::to_common(&ClaudeCode::from_text(&text)?)?,
        }
    };
    Ok(common)
}

pub(crate) fn builtin_target(id: &str) -> bool {
    matches!(id, "codex" | "claude-desktop" | "deepseek")
}
pub(crate) fn check_target(cli: &Cli, id: &str) -> Result<()> {
    if builtin_target(id) {
        Ok(())
    } else {
        crate::protocol::require(cli, id, "target")
    }
}

pub(crate) fn prepare(
    cli: &Cli,
    target_agent: &str,
    common: &mut txcript::Transcript<txcript::Common>,
    staging: &Path,
    id: &str,
    target_id: &str,
) -> Result<(Vec<u8>, PathBuf, txcript::Transcript<txcript::Common>)> {
    if !builtin_target(target_agent) {
        let path = crate::protocol::target(cli, target_agent, target_id)?;
        let bytes = crate::protocol::encode(common)?;
        let delivered = crate::protocol::decode(target_id, &bytes)?;
        return Ok((bytes, path, delivered));
    }
    let (bytes, target) = match target_agent {
        "deepseek" => return deepseek(cli)?.prepare(&common, staging),
        "codex" => {
            // Target configuration chooses the runtime model on resume.
            common.meta.model = None;
            common.meta.cli_version = None;
            for msg in &mut common.body {
                msg.model = None;
            }
            let converted = Codex::from_common(&common)?;
            let bytes = Codex::to_text(&converted)?.into_bytes();
            let saved = CodexStore::new(staging).save(&converted)?;
            (
                bytes,
                codex(cli)?
                    .sessions_dir
                    .join(saved.reference.strip_prefix(staging)?),
            )
        }
        _ => {
            let converted = ClaudeCode::from_common(&common)?;
            let mut text = ClaudeCode::to_text(&converted)?;
            text.push_str(&serde_json::to_string(&json!({
                "type": "custom-title", "sessionId": target_id,
                "customTitle": format!("Handoff {}", &id[..8])
            }))?);
            text.push('\n');
            let saved = ClaudeStore::new(staging).save(&converted)?;
            (
                text.into_bytes(),
                claude(cli)?
                    .root
                    .join(saved.reference.strip_prefix(staging)?),
            )
        }
    };
    // The delivered manifest describes what the target stores, not the
    // pre-conversion transcript: codecs may normalize tool payloads.
    let delivered_agent = if target_agent == "claude-desktop" {
        "claude"
    } else {
        target_agent
    };
    let delivered = load_common(cli, delivered_agent, target_id, &bytes)?;
    Ok((bytes, target, delivered))
}

// Plans written before the delivered manifest existed can still be checked
// against the native artifact kept in their plan directory.
pub(crate) fn read_prepared(
    cli: &Cli,
    target_agent: &str,
    target_id: &str,
    prepared: &Path,
) -> Result<txcript::Transcript<txcript::Common>> {
    if target_agent == "deepseek" {
        let staging = prepared.parent().context("缺少计划目录")?.join("staging");
        ensure!(staging.is_dir(), "缺少计划准备目录");
        let mut config = cli.clone();
        config.dsh_root = Some(staging);
        return deepseek(&config)?.read(target_id);
    }
    let source_agent = if target_agent == "claude-desktop" {
        "claude"
    } else {
        target_agent
    };
    let bytes = std::fs::read(prepared)?;
    load_common(cli, source_agent, target_id, &bytes)
}
pub(crate) fn open(agent: &str, id: &str, cwd: &Path) -> Result<()> {
    if !builtin_target(agent) {
        return Err(crate::error::fault(
            "MANUAL_OPEN_REQUIRED",
            "由协议接收端打开会话",
            "读取 target_path，在 workspace.cwd 恢复 target_id。",
            false,
        ));
    }
    if agent == "deepseek" {
        return Err(crate::error::fault(
            "MANUAL_OPEN_REQUIRED",
            "请在现有 Harness 页面选择导入会话",
            "按 next_action 刷新现有 Harness 页面，核对目标 ID 和原工作目录。",
            false,
        ));
    }
    let status = if agent == "codex" {
        Command::new("open")
            .arg(format!("codex://threads/{id}"))
            .output()
            .context(crate::error::fault(
                "CLIENT_UNAVAILABLE",
                "客户端启动程序不可用",
                "检查客户端安装，或按 next_action 使用同一目标 ID 手动打开。",
                true,
            ))?
            .status
    } else {
        Command::new(std::env::var("HANDOFF_CLAUDE_BIN").unwrap_or_else(|_| "claude".into()))
            .current_dir(cwd)
            .args(["--desktop", "--resume", id])
            .output()
            .context(crate::error::fault(
                "CLIENT_UNAVAILABLE",
                "客户端启动程序不可用",
                "检查客户端安装，或按 next_action 使用同一目标 ID 手动打开。",
                true,
            ))?
            .status
    };
    ensure!(
        status.success(),
        crate::error::fault(
            "CLIENT_OPEN_FAILED",
            "客户端打开失败",
            "按 next_action 使用同一目标 ID 手动恢复，或修复启动程序后重试。",
            true
        )
    );
    Ok(())
}

pub(crate) fn next_action(agent: &str, target_id: &str, plan_id: &str, cwd: &Path) -> String {
    if !builtin_target(agent) {
        format!(
            "由 {agent} 读取 target_path，恢复会话 {target_id}，使用工作目录 {}。",
            cwd.display()
        )
    } else if agent == "codex" {
        format!(
            "打开 codex://threads/{}，核对目录 {}",
            target_id,
            cwd.display()
        )
    } else if agent == "deepseek" {
        format!(
            "刷新现有 DeepSeek Harness 页面，在未分组会话中打开新记录（首次可能显示未命名），核对标题 Handoff {}、会话 {} 和目录 {}。",
            &plan_id[..8],
            target_id,
            cwd.display()
        )
    } else {
        format!(
            "在 Claude Desktop Code 的 /resume 中搜索 Handoff {}，选择会话 {}，核对目录 {}。首次使用先选择并信任该目录。",
            &plan_id[..8],
            target_id,
            cwd.display()
        )
    }
}
