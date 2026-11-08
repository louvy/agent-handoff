use anyhow::{Context, Result, ensure};
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Component, Path, PathBuf},
    time::Duration,
};

fn fault(code: &'static str, message: &'static str) -> anyhow::Error {
    crate::error::fault(
        code,
        message,
        "核对输入与状态目录，恢复文件或权限后重试。",
        code == "BACKUP_CHANGED",
    )
}
pub(crate) fn absolute(path: &Path) -> Result<PathBuf> {
    let path = std::path::absolute(path)?;
    let mut out = PathBuf::new();
    for part in path.components() {
        match part {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            part => out.push(part.as_os_str()),
        }
    }
    Ok(out)
}
pub(crate) fn migrate(db: &mut Connection) -> Result<()> {
    let tx = db.transaction()?;
    for (table, columns) in [
        (
            "plans",
            vec![
                ("task_id", "task_id"),
                ("target_id", "target_id"),
                ("target_path", "target"),
                ("version_id", "version_id"),
                ("parent_version_id", "parent_version_id"),
                ("created_at", "created_at"),
            ],
        ),
        (
            "versions",
            vec![
                ("task_id", "task_id"),
                ("source_agent", "source_agent"),
                ("source_id", "source_id"),
                ("source_path", "source"),
                ("created_at", "created_at"),
                ("parent_version_id", "parent_version_id"),
            ],
        ),
        (
            "assignments",
            vec![
                ("recipient", "recipient"),
                ("version_id", "version_id"),
                ("created_at", "created_at"),
            ],
        ),
    ] {
        let mut stmt = tx.prepare(&format!("PRAGMA table_xinfo({table})"))?;
        let existing = stmt
            .query_map([], |r| r.get::<_, String>(1))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        drop(stmt);
        for (column, key) in columns {
            if !existing.iter().any(|c| c == column) {
                tx.execute_batch(&format!("ALTER TABLE {table} ADD COLUMN {column} TEXT GENERATED ALWAYS AS (json_extract(body,'$.{key}')) VIRTUAL;"))?;
            }
        }
    }
    tx.execute_batch(
        "CREATE INDEX IF NOT EXISTS plan_target ON plans(target_id,target_path);
        CREATE INDEX IF NOT EXISTS plan_task ON plans(task_id);
        CREATE INDEX IF NOT EXISTS version_source ON versions(source_agent,source_id,source_path);
        CREATE INDEX IF NOT EXISTS version_task ON versions(task_id);
        CREATE INDEX IF NOT EXISTS assignment_recipient ON assignments(recipient);
        CREATE TABLE IF NOT EXISTS gc_runs(id TEXT PRIMARY KEY); CREATE TABLE IF NOT EXISTS archives(task_id TEXT PRIMARY KEY, archived_at TEXT NOT NULL);",
    )?;
    tx.commit()?;
    Ok(())
}
pub(crate) fn page(
    db: &Connection,
    table: &str,
    column: &str,
    filter: Option<&str>,
    limit: usize,
    offset: usize,
    cursor: Option<i64>,
) -> Result<(Vec<String>, usize, Option<i64>)> {
    ensure!(
        (1..=1000).contains(&limit) && offset <= i64::MAX as usize,
        fault("INVALID_REQUEST", "分页超出范围")
    );
    // Generated columns are optional on databases last written by older binaries.
    let expr = format!("json_extract(body,'$.{column}')");
    let has_column: bool = db
        .prepare(&format!("PRAGMA table_xinfo({table})"))?
        .query_map([], |r| r.get::<_, String>(1))?
        .collect::<rusqlite::Result<Vec<_>>>()?
        .iter()
        .any(|c| c == column);
    let field = if has_column { column } else { &expr };
    let predicate = if filter.is_some() {
        format!("WHERE {field}=?1")
    } else {
        "WHERE ?1 IS NULL".into()
    };
    let total: i64 = db.query_row(
        &format!("SELECT COUNT(*) FROM {table} {predicate}"),
        [filter],
        |r| r.get(0),
    )?;
    let mut stmt = db.prepare(&format!(
        "SELECT body,rowid FROM {table} {predicate} AND (?4 IS NULL OR rowid < ?4) ORDER BY rowid DESC LIMIT ?2 OFFSET ?3"
    ))?;
    let rows = stmt
        .query_map(
            params![filter, (limit + 1) as i64, offset as i64, cursor],
            |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)),
        )?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let next = if rows.len() > limit {
        Some(rows[limit - 1].1)
    } else {
        None
    };
    Ok((
        rows.into_iter().take(limit).map(|(body, _)| body).collect(),
        total as usize,
        next,
    ))
}
fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn inventory(root: &Path) -> Result<BTreeMap<PathBuf, String>> {
    fn visit(root: &Path, dir: &Path, out: &mut BTreeMap<PathBuf, String>) -> Result<()> {
        for entry in fs::read_dir(dir)? {
            let entry = entry?;
            let path = entry.path();
            let ty = entry.file_type()?;
            ensure!(
                !ty.is_symlink(),
                fault("BACKUP_INVALID", "状态备份中包含符号链接")
            );
            if ty.is_dir() {
                visit(root, &path, out)?
            } else {
                ensure!(ty.is_file(), fault("BACKUP_INVALID", "备份包含特殊文件"));
                out.insert(
                    path.strip_prefix(root)?.to_path_buf(),
                    hash(&fs::read(path)?),
                );
            }
        }
        Ok(())
    }
    let mut out = BTreeMap::new();
    if root.exists() {
        ensure!(
            fs::symlink_metadata(root)?.is_dir(),
            fault("BACKUP_INVALID", "状态子目录必须为真实目录")
        );
    }
    if root.exists() {
        visit(root, root, &mut out)?
    }
    Ok(out)
}
fn state_files(state: &Path) -> Result<BTreeMap<PathBuf, String>> {
    let mut files = BTreeMap::new();
    for entry in fs::read_dir(state)? {
        let entry = entry?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name == "agents" || uuid::Uuid::parse_str(&name).is_ok() {
            for (path, digest) in inventory(&entry.path())? {
                files.insert(Path::new(name.as_ref()).join(path), digest);
            }
        }
    }
    if state.join(".handoff-state").exists() {
        files.insert(
            ".handoff-state".into(),
            hash(&fs::read(state.join(".handoff-state"))?),
        );
    }
    Ok(files)
}
#[derive(Serialize, Deserialize)]
struct Manifest {
    format: u32,
    state: PathBuf,
    state_id: String,
    files: BTreeMap<PathBuf, String>,
}
fn snapshot(db: &Connection, state: &Path, destination: &Path) -> Result<()> {
    let before = state_files(state)?;
    db.backup("main", destination.join("handoff.sqlite3"), None)?;
    for path in before.keys() {
        let target = destination.join(path);
        crate::app::private_dir(target.parent().context("无父目录")?)?;
        fs::copy(state.join(path), target)?;
    }
    ensure!(
        before == state_files(state)?,
        fault("BACKUP_CHANGED", "备份期间协议文件发生变化，请重试")
    );
    let mut files = before;
    files.insert(
        "handoff.sqlite3".into(),
        hash(&fs::read(destination.join("handoff.sqlite3"))?),
    );
    // Verify copied files too, including files updated during their copy.
    ensure!(
        files == inventory(destination)?,
        fault("BACKUP_CHANGED", "备份文件复制期间发生变化，请重试")
    );
    let state_id = db.query_row("SELECT value FROM metadata WHERE key='state_id'", [], |r| {
        r.get(0)
    })?;
    let manifest = Manifest {
        format: 1,
        state: state.into(),
        state_id,
        files,
    };
    let bytes = serde_json::to_vec_pretty(&manifest)?;
    fs::write(destination.join("manifest.json"), bytes)?;
    for path in inventory(destination)?.keys() {
        fs::File::open(destination.join(path))?.sync_all()?;
    }
    fs::File::open(destination)?.sync_all()?;
    Ok(())
}
pub(crate) fn backup(db: &mut Connection, state: &Path, path: &Path) -> Result<Value> {
    let destination = absolute(path)?;
    ensure!(
        !destination.starts_with(state) && !destination.exists(),
        fault("BACKUP_CONFLICT", "备份路径必须是状态目录外的空闲路径")
    );

    let parent = destination.parent().context("无备份父目录")?;
    let stage = tempfile::Builder::new()
        .prefix(".handoff-backup-")
        .tempdir_in(parent)?;
    snapshot(db, state, stage.path())?;
    fs::rename(stage.path(), &destination)?;
    fs::File::open(parent)?.sync_all()?;
    Ok(
        json!({"backup":destination,"external_dependencies":"原 worktree 与内置客户端原生会话目录需要单独保留"}),
    )
}
fn validate_backup(path: &Path) -> Result<Manifest> {
    let manifest: Manifest = serde_json::from_slice(&fs::read(path.join("manifest.json"))?)
        .map_err(|_| fault("BACKUP_INVALID", "备份清单损坏"))?;
    ensure!(
        manifest.format == 1,
        fault("BACKUP_INVALID", "备份版本不受支持")
    );
    for key in manifest.files.keys() {
        ensure!(
            !key.is_absolute() && key.components().all(|c| matches!(c, Component::Normal(_))),
            fault("BACKUP_INVALID", "备份清单路径无效")
        );
    }
    let mut files = inventory(path)?;
    files.remove(Path::new("manifest.json"));
    ensure!(
        files == manifest.files,
        fault("BACKUP_INVALID", "备份文件缺失或摘要不符")
    );
    let db = Connection::open_with_flags(
        path.join("handoff.sqlite3"),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )?;
    let check: String = db.query_row("PRAGMA integrity_check", [], |r| r.get(0))?;
    let identity: String =
        db.query_row("SELECT value FROM metadata WHERE key='state_id'", [], |r| {
            r.get(0)
        })?;
    ensure!(
        check == "ok" && identity == manifest.state_id,
        fault("BACKUP_INVALID", "备份数据库完整性校验失败")
    );
    Ok(manifest)
}
pub(crate) fn control_path(state: &Path, suffix: &str) -> Result<PathBuf> {
    let name = state
        .file_name()
        .context("无状态目录名称")?
        .to_str()
        .context("状态目录名称无效")?;
    Ok(state
        .parent()
        .context("无状态父目录")?
        .join(format!(".{name}.handoff-{suffix}")))
}

fn database_files(database: &Path) -> Result<(Vec<u8>, Option<Vec<u8>>, Option<Vec<u8>>)> {
    let main = fs::read(database)?;
    let companion = |suffix: &str| -> Result<Option<Vec<u8>>> {
        match fs::read(database.with_file_name(format!("handoff.sqlite3-{suffix}"))) {
            Ok(bytes) => Ok(Some(bytes)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e.into()),
        }
    };
    Ok((main, companion("wal")?, companion("journal")?))
}
// Read stable copies, including committed WAL frames, without opening the source
// in SQLite. SQLite may create auxiliary files only inside the private tempdir.
pub(crate) fn read_snapshot(database: &Path) -> Result<Connection> {
    for _ in 0..3 {
        let before = database_files(database)?;
        let temp = tempfile::Builder::new().prefix("handoff-read-").tempdir()?;
        let copy = temp.path().join("handoff.sqlite3");
        fs::write(&copy, &before.0)?;
        if let Some(wal) = &before.1 {
            fs::write(temp.path().join("handoff.sqlite3-wal"), wal)?;
        }
        if let Some(journal) = &before.2 {
            fs::write(temp.path().join("handoff.sqlite3-journal"), journal)?;
        }
        if before != database_files(database)? {
            continue;
        }
        let copied = Connection::open(copy)?;
        let mut memory = Connection::open_in_memory()?;
        {
            let backup = rusqlite::backup::Backup::new(&copied, &mut memory)?;
            backup.run_to_completion(64, Duration::from_millis(1), None)?;
        }
        if before == database_files(database)? {
            return Ok(memory);
        }
    }
    Err(crate::error::fault(
        "STATE_BUSY",
        "读取期间状态库持续变化",
        "稍后重试查询。",
        true,
    ))
}

#[derive(Serialize, Deserialize)]
struct RestoreJournal {
    state: PathBuf,
    staged: PathBuf,
    rollback: PathBuf,
}
fn persist_journal(path: &Path, journal: &RestoreJournal) -> Result<()> {
    let temp = tempfile::NamedTempFile::new_in(path.parent().context("无日志父目录")?)?;
    fs::write(temp.path(), serde_json::to_vec(journal)?)?;
    temp.as_file().sync_all()?;
    temp.persist(path).map_err(|e| e.error)?;
    fs::File::open(path.parent().context("无日志父目录")?)?.sync_all()?;
    Ok(())
}
// Caller holds the stable sibling writer lock. Queries never repair state.
pub(crate) fn recover_restore(state: &Path) -> Result<()> {
    let path = control_path(state, "restore.json")?;
    if !path.try_exists()? {
        return Ok(());
    }
    let journal: RestoreJournal = serde_json::from_slice(&fs::read(&path)?)?;
    let parent = state.parent().context("无状态父目录")?;
    ensure!(
        journal.state == state
            && journal.staged.parent() == Some(parent)
            && journal.rollback.parent() == Some(parent)
            && journal
                .staged
                .file_name()
                .is_some_and(|n| n.to_string_lossy().starts_with(".handoff-restore-"))
            && journal
                .rollback
                .file_name()
                .is_some_and(|n| n.to_string_lossy().starts_with("handoff-rollback-")),
        fault("STATE_INVALID", "恢复日志路径不属于当前状态目录")
    );
    if !state.exists() && journal.rollback.exists() {
        fs::rename(&journal.rollback, state)?;
    }
    if journal.staged.exists() {
        fs::remove_dir_all(&journal.staged)?;
    }
    fs::remove_file(path)?;
    fs::File::open(parent)?.sync_all()?;
    Ok(())
}
fn copy_tree(source: &Path, destination: &Path) -> Result<()> {
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let ty = entry.file_type()?;
        let dest = destination.join(entry.file_name());
        ensure!(
            !ty.is_symlink(),
            fault("RESTORE_CONFLICT", "状态目录包含符号链接")
        );
        if ty.is_dir() {
            crate::app::private_dir(&dest)?;
            copy_tree(&entry.path(), &dest)?;
        } else {
            ensure!(
                ty.is_file(),
                fault("RESTORE_CONFLICT", "状态目录包含特殊文件")
            );
            fs::copy(entry.path(), dest)?;
        }
    }
    Ok(())
}
fn restore_protocol(current: &[u8], saved: &[u8], id: &str) -> Result<Vec<u8>> {
    let saved_session = match crate::protocol::decode(id, saved) {
        Ok(value) => value,
        Err(_) => {
            return Ok(if crate::protocol::decode(id, current).is_ok() {
                current.to_vec()
            } else {
                saved.to_vec()
            });
        }
    };
    let current_session = match crate::protocol::decode(id, current) {
        Ok(value) => value,
        // The corrupt local copy is preserved in the rollback tree.
        Err(_) => return Ok(saved.to_vec()),
    };
    ensure!(
        saved_session.meta.cwd == current_session.meta.cwd,
        fault("RESTORE_CONFLICT", "协议会话工作目录发生分叉")
    );
    if saved_session.body.starts_with(&current_session.body) {
        return Ok(saved.to_vec());
    }
    if current_session.body.starts_with(&saved_session.body) {
        return Ok(current.to_vec());
    }
    Err(fault("RESTORE_CONFLICT", "协议会话历史发生分叉"))
}
pub(crate) fn restore(
    state: &Path,
    path: &Path,
    seconds: u64,
    cancelled: Option<&std::sync::atomic::AtomicBool>,
) -> Result<Value> {
    let source = path.canonicalize()?;
    ensure!(
        !source.starts_with(state),
        fault("BACKUP_CONFLICT", "恢复来源必须位于状态目录外")
    );
    let parent = state.parent().context("无状态父目录")?;
    let _lock = crate::app::operation_lock(state, seconds, cancelled)?;
    recover_restore(state)?;
    let backup = tempfile::Builder::new()
        .prefix(".handoff-backup-read-")
        .tempdir_in(parent)?;
    copy_tree(&source, backup.path())?;
    let manifest = validate_backup(backup.path())?;
    ensure!(
        manifest.state == state,
        fault(
            "RESTORE_LOCATION_MISMATCH",
            "请恢复到备份所记录的原状态目录"
        )
    );
    let database = state.join("handoff.sqlite3");
    let current = if database.try_exists()? {
        match read_snapshot(&database) {
            Ok(db) => {
                let initialized: bool = db.query_row(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='metadata')",
                    [],
                    |r| r.get(0),
                )?;
                if initialized {
                    let identity: Option<String> = db
                        .query_row("SELECT value FROM metadata WHERE key='state_id'", [], |r| {
                            r.get(0)
                        })
                        .optional()?;
                    if let Some(identity) = identity {
                        ensure!(
                            identity == manifest.state_id,
                            fault("RESTORE_IDENTITY_MISMATCH", "备份属于不同状态库")
                        );
                        Some(db)
                    } else {
                        None
                    }
                } else {
                    None
                }
            }
            Err(error) => {
                let code = crate::error::response(&error)["code"]
                    .as_str()
                    .unwrap_or("")
                    .to_string();
                ensure!(code == "STATE_CORRUPT", error);
                None
            }
        }
    } else {
        None
    };
    // Everything below is built off to the side; conflicts leave the live tree intact.
    let before = if state.exists() {
        inventory(state)?
    } else {
        BTreeMap::new()
    };
    let staged = tempfile::Builder::new()
        .prefix(".handoff-restore-")
        .tempdir_in(parent)?;
    if state.exists() {
        copy_tree(state, staged.path())?;
    }
    if let Some(current) = &current {
        let has_gc: bool = current.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='gc_runs')",
            [],
            |r| r.get(0),
        )?;
        if has_gc {
            recover_gc(current, staged.path())?;
        }
    }
    for suffix in [
        "handoff.sqlite3",
        "handoff.sqlite3-wal",
        "handoff.sqlite3-shm",
    ] {
        let file = staged.path().join(suffix);
        if file.exists() {
            fs::remove_file(file)?;
        }
    }
    fs::copy(
        backup.path().join("handoff.sqlite3"),
        staged.path().join("handoff.sqlite3"),
    )?;
    for rel in manifest
        .files
        .keys()
        .filter(|p| p.as_path() != Path::new("handoff.sqlite3"))
    {
        let dest = staged.path().join(rel);
        let saved = fs::read(backup.path().join(rel))?;
        crate::app::private_dir(dest.parent().context("无恢复父目录")?)?;
        let bytes = if dest.try_exists()? {
            ensure!(
                fs::symlink_metadata(&dest)?.is_file(),
                fault("RESTORE_CONFLICT", "恢复目标被其他类型路径占用")
            );
            let existing = fs::read(&dest)?;
            if rel.starts_with("agents") && rel.extension().is_some_and(|v| v == "json") {
                let id = rel
                    .file_stem()
                    .and_then(|s| s.to_str())
                    .context("协议文件名无效")?;
                restore_protocol(&existing, &saved, id)?
            } else {
                ensure!(
                    existing == saved,
                    fault("RESTORE_CONFLICT", "现有计划产物与备份冲突")
                );
                existing
            }
        } else {
            saved
        };
        fs::write(dest, bytes)?;
    }
    let snapshot_db = Connection::open(staged.path().join("handoff.sqlite3"))?;
    let agents = snapshot_db
        .prepare("SELECT id FROM protocol_agents")?
        .query_map([], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for agent in agents {
        ensure!(
            !agent.is_empty()
                && agent
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-'),
            fault("BACKUP_INVALID", "备份中的接入标识无效")
        );
        crate::app::private_dir(&staged.path().join("agents").join(agent))?;
    }
    let delivered = snapshot_db
        .prepare("SELECT body FROM plans WHERE status!='previewed'")?
        .query_map([], |r| r.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for body in delivered {
        let plan: Value = serde_json::from_str(&body)?;
        let agent = plan["target_agent"].as_str().unwrap_or("claude-desktop");
        if crate::agent::builtin_target(agent) {
            continue;
        }
        let target = Path::new(plan["target"].as_str().context("计划缺少目标路径")?)
            .strip_prefix(state)
            .map_err(|_| fault("BACKUP_INVALID", "协议目标路径超出备份状态目录"))?;
        let prepared = Path::new(plan["prepared"].as_str().context("计划缺少产物路径")?)
            .strip_prefix(state)
            .map_err(|_| fault("BACKUP_INVALID", "计划产物路径超出备份状态目录"))?;
        let prepared = fs::read(staged.path().join(prepared))
            .map_err(|_| fault("BACKUP_INVALID", "协议目标的交付基线缺失"))?;
        ensure!(
            Some(hash(&prepared).as_str()) == plan["prepared_digest"].as_str(),
            fault("BACKUP_INVALID", "协议目标交付基线摘要不符")
        );
        let current = fs::read(staged.path().join(target))
            .map_err(|_| fault("BACKUP_INVALID", "协议目标会话缺失"))?;
        ensure!(
            crate::protocol::continued(
                plan["target_id"].as_str().context("缺少目标 ID")?,
                &prepared,
                &current
            ),
            fault("RESTORE_CONFLICT", "恢复候选缺少协议目标的已交付历史")
        );
    }
    let check: String = snapshot_db.query_row("PRAGMA integrity_check", [], |r| r.get(0))?;
    ensure!(
        check == "ok",
        fault("BACKUP_INVALID", "恢复数据库完整性校验失败")
    );
    drop(snapshot_db);
    // Generate a usable rollback backup for healthy state and retain the full
    // original tree separately, including damaged files and unindexed plans.
    let rollback = parent.join(format!("handoff-rollback-{}", uuid::Uuid::new_v4()));
    let rollback_backup = if let Some(current) = current {
        let destination = parent.join(format!("handoff-backup-{}", uuid::Uuid::new_v4()));
        crate::app::private_dir(&destination)?;
        snapshot(&current, state, &destination)?;
        Some(destination)
    } else {
        None
    };
    ensure!(
        before
            == if state.exists() {
                inventory(state)?
            } else {
                BTreeMap::new()
            },
        crate::error::fault(
            "RESTORE_CHANGED",
            "恢复准备期间本地文件发生变化",
            "停止客户端写入后重试恢复。",
            true
        )
    );
    for rel in inventory(staged.path())?.keys() {
        fs::File::open(staged.path().join(rel))?.sync_all()?;
    }
    fs::File::open(staged.path())?.sync_all()?;
    let staged = staged.keep();
    let journal = RestoreJournal {
        state: state.into(),
        staged: staged.clone(),
        rollback: rollback.clone(),
    };
    persist_journal(&control_path(state, "restore.json")?, &journal)?;
    let had_state = state.exists();
    let mut moved_original = false;
    let mut installed = false;
    let activation = (|| -> Result<()> {
        if had_state {
            fs::rename(state, &rollback)?;
            moved_original = true;
        }
        fs::rename(&staged, state)?;
        installed = true;
        fs::File::open(parent)?.sync_all()?;
        fs::remove_file(control_path(state, "restore.json")?)?;
        fs::File::open(parent)?.sync_all()?;
        Ok(())
    })();
    if let Err(error) = activation {
        if installed {
            fs::rename(state, &staged)?;
        }
        if moved_original {
            fs::rename(&rollback, state)?;
        }
        if staged.exists() {
            fs::remove_dir_all(&staged)?;
        }
        let journal = control_path(state, "restore.json")?;
        if journal.exists() {
            fs::remove_file(journal)?;
        }
        return Err(error);
    }
    Ok(
        json!({"restored":source,"state":state,"rollback_backup":rollback_backup,
        "rollback_state":rollback.exists().then_some(rollback),"preserved_later_files":true}),
    )
}
pub(crate) fn archive(db: &mut Connection, _state: &Path, task: &str) -> Result<Value> {
    let tx = db.transaction()?;
    let exists:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM versions WHERE task_id=? UNION ALL SELECT 1 FROM plans WHERE task_id=?)",params![task,task],|r|r.get(0))?;
    ensure!(exists, fault("TASK_NOT_FOUND", "任务不存在"));
    let pending:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM assignments a JOIN versions v ON a.version_id=v.id WHERE v.task_id=? AND a.plan_id IS NULL)",[task],|r|r.get(0))?;
    ensure!(
        !pending,
        fault("TASK_PENDING", "任务仍有待领取指派，请先处理指派")
    );
    tx.execute(
        "INSERT OR IGNORE INTO archives VALUES(?,?)",
        params![task, chrono::Utc::now().to_rfc3339()],
    )?;
    tx.commit()?;
    Ok(json!({"task_id":task,"archived":true}))
}
pub(crate) fn gc(db: &mut Connection, state: &Path, age: &str, execute: bool) -> Result<Value> {
    let days = age
        .strip_suffix('d')
        .and_then(|s| s.parse::<u32>().ok())
        .ok_or_else(|| fault("INVALID_REQUEST", "保留期限应为天数，例如 30d"))?;
    let cutoff = (chrono::Utc::now() - chrono::Duration::days(days as i64)).to_rfc3339();
    let exists: bool = db.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='archives')",
        [],
        |r| r.get(0),
    )?;
    if !exists {
        return Ok(json!({"dry_run":!execute,"plans":[],"versions":[],"bytes":0}));
    }
    let mut stmt=db.prepare("SELECT p.id,p.body FROM plans p JOIN archives a ON p.task_id=a.task_id WHERE a.archived_at<=?")?;
    let eligible = stmt
        .query_map([&cutoff], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    drop(stmt);
    let mut plans = BTreeSet::new();
    for (id, body) in &eligible {
        let value: Value = serde_json::from_str(body)?;
        let pending: bool = db.query_row(
            "SELECT EXISTS(SELECT 1 FROM assignments WHERE version_id=? AND plan_id IS NULL)",
            [value["version_id"].as_str()],
            |r| r.get(0),
        )?;
        if !pending {
            plans.insert(id.clone());
        }
    }
    // A live descendant retains the plans needed to explain its provenance.
    let plan_bodies = db
        .prepare("SELECT id,body FROM plans")?
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?
        .collect::<rusqlite::Result<BTreeMap<_, _>>>()?;
    loop {
        let count = plans.len();
        for (id, body) in &plan_bodies {
            if !plans.contains(id) {
                let value: Value = serde_json::from_str(body)?;
                if let Some(parent) = value["parent_plan_id"].as_str() {
                    plans.remove(parent);
                }
            }
        }
        if count == plans.len() {
            break;
        }
    }
    let all_versions = db
        .prepare("SELECT id,body FROM versions")?
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?
        .collect::<rusqlite::Result<BTreeMap<_, _>>>()?;
    let mut protected = BTreeSet::new();
    for body in db
        .prepare("SELECT id,body FROM plans")?
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?
    {
        let (id, body) = body?;
        if !plans.contains(&id) {
            let v: Value = serde_json::from_str(&body)?;
            for key in ["version_id", "parent_version_id"] {
                if let Some(id) = v[key].as_str() {
                    protected.insert(id.to_string());
                }
            }
        }
    }
    for row in db
        .prepare("SELECT version_id,plan_id FROM assignments")?
        .query_map([], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?))
        })?
    {
        let (version, plan) = row?;
        if plan.as_ref().is_none_or(|id| !plans.contains(id)) {
            protected.insert(version);
        }
    }
    for (id, body) in &all_versions {
        let v: Value = serde_json::from_str(body)?;
        let archived: bool = db.query_row(
            "SELECT EXISTS(SELECT 1 FROM archives WHERE task_id=? AND archived_at<=?)",
            params![v["task_id"].as_str(), cutoff],
            |r| r.get(0),
        )?;
        if !archived {
            protected.insert(id.clone());
        }
    }
    loop {
        let before = (protected.len(), plans.len());
        for id in protected.clone() {
            if let Some(body) = all_versions.get(&id) {
                let value: Value = serde_json::from_str(body)?;
                if let Some(parent) = value["parent_version_id"].as_str() {
                    protected.insert(parent.into());
                }
                if let Some(parent) = value["parent_plan_id"].as_str() {
                    plans.remove(parent);
                }
            }
        }
        for (id, body) in &plan_bodies {
            if !plans.contains(id) {
                let value: Value = serde_json::from_str(body)?;
                if let Some(parent) = value["parent_plan_id"].as_str() {
                    plans.remove(parent);
                }
                for key in ["version_id", "parent_version_id"] {
                    if let Some(version) = value[key].as_str() {
                        protected.insert(version.into());
                    }
                }
            }
        }
        if before == (protected.len(), plans.len()) {
            break;
        }
    }
    let versions = all_versions
        .keys()
        .filter(|id| !protected.contains(*id))
        .cloned()
        .collect::<Vec<_>>();
    let mut bytes = 0u64;
    for id in &plans {
        ensure!(
            uuid::Uuid::parse_str(id).is_ok(),
            fault("STATE_INVALID", "计划 ID 无效")
        );
        let folder = state.join(id);
        if folder.exists() {
            ensure!(
                folder.canonicalize()? == folder,
                fault("STATE_INVALID", "计划目录路径异常")
            );
            for (rel, _) in inventory(&folder)? {
                bytes += fs::metadata(folder.join(rel))?.len();
            }
        }
    }
    if execute {
        // Quarantine first; any failed database transaction puts the directories back.
        let run = uuid::Uuid::new_v4().to_string();
        let quarantine = state.join(format!(".handoff-gc-{run}"));
        crate::app::private_dir(&quarantine)?;
        let state_id: String =
            db.query_row("SELECT value FROM metadata WHERE key='state_id'", [], |r| {
                r.get(0)
            })?;
        fs::write(
            quarantine.join("journal.json"),
            serde_json::to_vec(&json!({"state_id":state_id,"plans":plans}))?,
        )?;
        fs::File::open(quarantine.join("journal.json"))?.sync_all()?;
        fs::File::open(&quarantine)?.sync_all()?;
        let result = (|| -> Result<()> {
            let tx = db.transaction()?;
            for id in &plans {
                if state.join(id).exists() {
                    fs::rename(state.join(id), quarantine.join(id))?;
                }
                tx.execute("DELETE FROM assignments WHERE plan_id=?", [id])?;
                tx.execute("DELETE FROM plans WHERE id=?", [id])?;
            }
            for id in &versions {
                tx.execute("DELETE FROM versions WHERE id=?", [id])?;
            }
            fs::File::open(&quarantine)?.sync_all()?;
            fs::File::open(state)?.sync_all()?;
            tx.execute("INSERT INTO gc_runs VALUES(?)", [&run])?;
            tx.commit()?;
            Ok(())
        })();
        if result.is_err() {
            for id in &plans {
                let p = quarantine.join(id);
                if p.exists() {
                    fs::rename(p, state.join(id))?;
                }
            }
        }
        result?;
        fs::remove_dir_all(quarantine)?;
    }
    Ok(
        json!({"dry_run":!execute,"plans":plans,"versions":versions,"bytes":bytes,
        "reason":"仅清理显式归档且超过期限的对象，保留未知目录和客户端会话"}),
    )
}

pub(crate) fn recover_gc(db: &Connection, state: &Path) -> Result<()> {
    for entry in fs::read_dir(state)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().to_string();
        let Some(run) = name.strip_prefix(".handoff-gc-") else {
            continue;
        };
        if uuid::Uuid::parse_str(run).is_err() || !entry.file_type()?.is_dir() {
            continue;
        }
        let journal = entry.path().join("journal.json");
        if !journal.is_file() {
            continue;
        }
        let Ok(journal) = serde_json::from_slice::<Value>(&fs::read(journal)?) else {
            continue;
        };
        let identity: String =
            db.query_row("SELECT value FROM metadata WHERE key='state_id'", [], |r| {
                r.get(0)
            })?;
        if journal["state_id"].as_str() != Some(identity.as_str()) {
            continue;
        }
        let plans: BTreeSet<String> = serde_json::from_value(journal["plans"].clone())?;
        ensure!(
            plans.iter().all(|s| uuid::Uuid::parse_str(s).is_ok()),
            fault("STATE_INVALID", "清理恢复清单无效")
        );
        let committed: bool = db.query_row(
            "SELECT EXISTS(SELECT 1 FROM gc_runs WHERE id=?)",
            [run],
            |r| r.get(0),
        )?;
        if !committed {
            for id in plans {
                let saved = entry.path().join(&id);
                if saved.exists() {
                    ensure!(
                        !state.join(&id).exists(),
                        fault("STATE_INVALID", "清理恢复目录冲突")
                    );
                    fs::rename(saved, state.join(id))?;
                }
            }
        }
        fs::remove_dir_all(entry.path())?;
    }
    Ok(())
}
