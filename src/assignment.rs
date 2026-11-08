use crate::app::{self, Cli};
use anyhow::{Context, Result, ensure};
use rusqlite::{Connection, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::path::Path;

#[derive(Serialize, Deserialize)]
struct Assignment {
    id: String,
    version_id: String,
    recipient: String,
    instructions: String,
    created_at: String,
}

fn check_recipient(recipient: &str) -> Result<()> {
    ensure!(
        matches!(recipient, "codex" | "claude-desktop" | "deepseek"),
        "不支持的接收端"
    );
    Ok(())
}

fn view(db: &Connection, assignment: &Assignment, plan_id: Option<&str>) -> Result<Value> {
    let mut value = serde_json::to_value(assignment)?;
    let plan = plan_id.map(|id| app::plan_view(db, id)).transpose()?;
    value["status"] = plan
        .as_ref()
        .map(|p| p["status"].clone())
        .unwrap_or(json!("pending"));
    value["plan"] = json!(plan);
    Ok(value)
}

pub(crate) fn assign(
    db: &Connection,
    version: &str,
    recipient: &str,
    instructions: &str,
) -> Result<Value> {
    check_recipient(recipient)?;
    ensure!(!instructions.trim().is_empty(), "请提供接手后的具体要求");
    let exists: bool = db.query_row(
        "SELECT EXISTS(SELECT 1 FROM versions WHERE id=?)",
        [version],
        |r| r.get(0),
    )?;
    ensure!(exists, "找不到独立版本");
    let assignment = Assignment {
        id: uuid::Uuid::new_v4().to_string(),
        version_id: version.into(),
        recipient: recipient.into(),
        instructions: instructions.into(),
        created_at: chrono::Utc::now().to_rfc3339(),
    };
    db.execute(
        "INSERT INTO assignments(id,body) VALUES(?,?)",
        params![assignment.id, serde_json::to_string(&assignment)?],
    )?;
    view(db, &assignment, None)
}

pub(crate) fn inbox(db: &Connection, recipient: &str) -> Result<Value> {
    check_recipient(recipient)?;
    let mut stmt = db.prepare("SELECT body,plan_id FROM assignments ORDER BY rowid DESC")?;
    let mut items = Vec::new();
    for row in stmt.query_map([], |r| {
        Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?))
    })? {
        let (body, plan_id) = row?;
        let item: Assignment = serde_json::from_str(&body)?;
        if item.recipient == recipient {
            items.push(view(db, &item, plan_id.as_deref())?);
        }
    }
    Ok(json!({"assignments":items}))
}

pub(crate) fn claim(
    cli: &Cli,
    db: &mut Connection,
    state: &Path,
    id: &str,
    recipient: &str,
    open: bool,
) -> Result<Value> {
    check_recipient(recipient)?;
    // Plan creation and assignment ownership commit together. A retry after a
    // crash resumes the committed plan, including a failed or unfinished publish.
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let (body, plan_id): (String, Option<String>) = tx
        .query_row(
            "SELECT body,plan_id FROM assignments WHERE id=?",
            [id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .context("找不到指派")?;
    let item: Assignment = serde_json::from_str(&body)?;
    ensure!(item.recipient == recipient, "该指派属于其他接收端");
    let mut preparation = None;
    let plan_id = match plan_id {
        Some(id) => id,
        None => {
            let (plan, guard) = app::prepare_version(
                cli,
                &tx,
                state,
                &item.version_id,
                recipient,
                false,
                Some(&item.instructions),
            )?;
            preparation = Some(guard);
            let id = plan["id"].as_str().context("计划缺少 ID")?.to_string();
            tx.execute(
                "UPDATE assignments SET plan_id=? WHERE id=?",
                params![id, item.id],
            )?;
            id
        }
    };
    if let Some(guard) = &mut preparation {
        guard.preserve();
    }
    tx.commit()?;
    if let Some(guard) = &mut preparation {
        guard.finish();
    }
    let plan = app::apply(cli, db, &plan_id, open)?;
    let mut result = view(db, &item, Some(&plan_id))?;
    result["plan"] = plan;
    Ok(result)
}
