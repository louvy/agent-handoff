use serde_json::{Value, json};
use std::fmt;

#[derive(Debug, serde::Deserialize)]
pub(crate) struct Fault {
    code: String,
    #[serde(rename = "error")]
    message: String,
    next_action: String,
    retryable: bool,
}
impl fmt::Display for Fault {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::error::Error for Fault {}

pub(crate) fn fault(
    code: &'static str,
    message: &'static str,
    next_action: &'static str,
    retryable: bool,
) -> anyhow::Error {
    Fault {
        code: code.into(),
        message: message.into(),
        next_action: next_action.into(),
        retryable,
    }
    .into()
}
// Discovery warnings already carry the public, classified failure.
pub(crate) fn from_response(value: &Value) -> Result<anyhow::Error, serde_json::Error> {
    Ok(serde_json::from_value::<Fault>(value.clone())?.into())
}

pub(crate) fn response(error: &anyhow::Error) -> Value {
    if let Some(inner) = error.downcast_ref::<anyhow::Error>() {
        return response(inner);
    }
    if let Some(f) = error.downcast_ref::<Fault>() {
        return json!({"error":f.message,"code":f.code,"next_action":f.next_action,"retryable":f.retryable});
    }
    // Known legacy validation messages are translated, never forwarded verbatim.
    let message = error.to_string();
    let (code, text, action, retry) = if message.contains("找不到独立版本") {
        (
            "VERSION_NOT_FOUND",
            "找不到独立版本",
            "查询 versions 并选择已有版本，或重新采集来源。",
            false,
        )
    } else if message.contains("找不到指派") {
        (
            "ASSIGNMENT_NOT_FOUND",
            "找不到指派",
            "查询对应接收端的 inbox，并使用完整指派 ID。",
            false,
        )
    } else if message.contains("找不到交接") || message.contains("找不到计划") {
        (
            "PLAN_NOT_FOUND",
            "找不到交接计划",
            "查询指派或重新预览，使用返回的完整计划 ID。",
            false,
        )
    } else if message.contains("属于其他接收端") {
        (
            "RECIPIENT_MISMATCH",
            "该指派属于其他接收端",
            "使用指派指定的接收端领取，或创建新的指派。",
            false,
        )
    } else if message.contains("来源已变化") {
        (
            "SOURCE_CHANGED",
            "来源已变化，请重新预览",
            "重新采集最新来源并预览，再发布目标。",
            false,
        )
    } else if message.contains("笔记已变化") {
        (
            "MEMORY_CHANGED",
            "绑定笔记已变化，请重新预览",
            "重新预览以保存最新笔记，再发布目标。",
            false,
        )
    } else if message.contains("工作树身份") {
        (
            "WORKSPACE_CHANGED",
            "工作树身份或 HEAD 已变化",
            "恢复原工作树，或重新采集当前工作树并创建新计划。",
            false,
        )
    } else if message.contains("来源缺少 cwd") {
        (
            "WORKSPACE_UNSPECIFIED",
            "来源缺少工作目录",
            "选择记录了工作目录的来源会话。",
            false,
        )
    } else if message.contains("cwd 不是目录") {
        (
            "WORKSPACE_INVALID",
            "工作目录不是文件夹",
            "恢复原工作目录后重试，或选择有效来源会话。",
            true,
        )
    } else if message.contains("不支持")
        || message.contains("只支持")
        || message.contains("limit 必须")
        || message.contains("请提供接手")
    {
        (
            "INVALID_REQUEST",
            "输入参数不符合当前接口要求",
            "核对客户端、模式、分页参数和接手要求；section 请选择 memory 或 transcript，再重新调用。",
            false,
        )
    } else if message.contains("目标 ID 已存在") {
        (
            "TARGET_CONFLICT",
            "目标 ID 已存在且内容不同",
            "保留现有目标，核对目标 ID，重新生成交接计划。",
            false,
        )
    } else if message.contains("计划产物已变化") {
        (
            "PLAN_CORRUPT",
            "计划产物已变化",
            "保留现有目标，从已保存版本重新生成计划。",
            false,
        )
    } else if let Some(e) = error.downcast_ref::<rusqlite::Error>() {
        match e {
            rusqlite::Error::SqliteFailure(e, _)
                if matches!(
                    e.code,
                    rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked
                ) =>
            {
                (
                    "STATE_BUSY",
                    "中转站存储正在使用中",
                    "稍后用同一计划或指派 ID 重试。",
                    true,
                )
            }
            rusqlite::Error::SqliteFailure(e, _)
                if matches!(
                    e.code,
                    rusqlite::ErrorCode::DatabaseCorrupt | rusqlite::ErrorCode::NotADatabase
                ) =>
            {
                (
                    "STATE_CORRUPT",
                    "中转站存储损坏或格式无效",
                    "保留现有数据，使用已知良好的备份恢复状态目录。",
                    false,
                )
            }
            rusqlite::Error::QueryReturnedNoRows => (
                "OBJECT_NOT_FOUND",
                "指定记录不存在",
                "查询版本、指派或交接状态，使用已有记录的完整 ID。",
                false,
            ),
            _ => (
                "STATE_UNAVAILABLE",
                "中转站存储暂时不可用",
                "检查状态目录的权限和剩余空间后，用同一 ID 重试。",
                true,
            ),
        }
    } else if let Some(e) = error.downcast_ref::<std::io::Error>() {
        match e.kind() {
            std::io::ErrorKind::PermissionDenied => (
                "ACCESS_DENIED",
                "无法访问所需文件或目录",
                "恢复状态目录、来源和目标目录的访问权限后，用同一 ID 重试。",
                true,
            ),
            std::io::ErrorKind::NotFound => (
                "FILE_UNAVAILABLE",
                "所需文件或程序不可用",
                "核对来源、计划文件和客户端安装路径，恢复后用同一 ID 重试。",
                true,
            ),
            std::io::ErrorKind::IsADirectory
            | std::io::ErrorKind::NotADirectory
            | std::io::ErrorKind::AlreadyExists => (
                "PATH_CONFLICT",
                "所需路径被其他文件或目录占用",
                "核对冲突路径并移走冲突项，保留现有会话，再用同一 ID 重试。",
                true,
            ),
            _ => (
                "IO_UNAVAILABLE",
                "文件操作未完成",
                "检查目录权限和剩余空间，恢复后用同一 ID 重试。",
                true,
            ),
        }
    } else if error.downcast_ref::<serde_json::Error>().is_some() {
        (
            "DATA_INVALID",
            "记录内容无法读取",
            "选择有效来源或已保存版本；状态记录损坏时从备份恢复。",
            false,
        )
    } else {
        (
            "REQUEST_FAILED",
            "当前操作无法完成",
            "核对客户端、完整记录 ID、输入参数及本地环境后重试。",
            false,
        )
    };
    response(&fault(code, text, action, retry))
}

pub(crate) fn target_invalid() -> anyhow::Error {
    fault(
        "TARGET_INVALID",
        "目标会话内容损坏或身份不匹配",
        "恢复该目标会话的有效备份后重试；需要重新交接时从已保存版本创建新计划。",
        false,
    )
}

pub(crate) fn plan_corrupt() -> anyhow::Error {
    fault(
        "PLAN_CORRUPT",
        "计划产物已变化",
        "保留现有目标，从已保存版本重新生成计划。",
        false,
    )
}
