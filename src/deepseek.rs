use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Write},
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};
use txcript::{Codec, Common, Discovered, TextCodec, Transcript, harness::simple::Simple};

// Run separately from the hub's process group so terminal cancellation and even
// SIGKILL of the hub cannot disable SDK cleanup. The supervisor owns/reaps Node;
// no session payload is parsed here, and all three streams pass straight through.
pub(crate) fn supervise(package: &Path, owner_pid: u32, timeout_seconds: u64) -> Result<i32> {
    let owner_alive = || unsafe { libc::getppid() as u32 == owner_pid };
    if !owner_alive() {
        return Ok(1);
    }
    let mut child = Command::new("node")
        .args([
            "--input-type=module",
            "-e",
            include_str!("../adapters/deepseek.mjs"),
        ])
        .arg(package)
        .process_group(0)
        .spawn()?;
    let started = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Ok(if status.success() { 0 } else { 1 }),
            Ok(None) if !owner_alive() => break Ok(1),
            Ok(None) if started.elapsed() >= Duration::from_secs(timeout_seconds) => break Ok(124),
            Ok(None) => std::thread::sleep(Duration::from_millis(20)),
            Err(error) => break Err(error.into()),
        }
    };
    // Node has a separate process group; kill helpers too, then reap Node before
    // exiting. This also closes inherited pipes when a helper outlives Node.
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGKILL);
    }
    let _ = child.wait();
    status
}

#[derive(Clone)]
pub(crate) struct DeepSeek {
    pub root: PathBuf,
    package: PathBuf,
    timeout: Duration,
}
impl DeepSeek {
    pub fn new(root: Option<&Path>, package: Option<&Path>, timeout_seconds: u64) -> Result<Self> {
        let base = std::env::var_os("DSH_HOME")
            .map(PathBuf::from)
            .unwrap_or(std::env::var_os("HOME").context("HOME 未设置")?.into());
        let root = root.map(Path::to_path_buf).unwrap_or_else(|| {
            if std::env::var_os("DSH_HOME").is_some() {
                base.join("sessions")
            } else {
                base.join(".dsh/sessions")
            }
        });
        let package = match package {
            Some(path) => path.canonicalize().context(crate::error::fault(
                "ADAPTER_UNAVAILABLE",
                "DeepSeek SDK 不可用",
                "安装兼容的 Harness，或用 --dsh-package 指向有效包目录后重试。",
                true,
            ))?,
            None => {
                let executable =
                    std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
                        .map(|p| p.join("dsh"))
                        .find(|p| p.is_file())
                        .context("找不到 dsh；请安装 Harness 或使用 --dsh-package 指定包目录")?
                        .canonicalize()?;
                executable
                    .parent()
                    .and_then(Path::parent)
                    .context("无法解析 dsh 包目录")?
                    .to_path_buf()
            }
        };
        Ok(Self {
            root: std::path::absolute(root)?,
            package,
            timeout: Duration::from_secs(timeout_seconds),
        })
    }
    fn call(&self, mut request: Value) -> Result<Value> {
        if request.get("root").is_none() {
            request["root"] = json!(self.root);
        }
        let mut child = Command::new(std::env::current_exe()?)
            .arg("__adapter-supervisor")
            .arg("--package")
            .arg(&self.package)
            .arg("--owner-pid")
            .arg(std::process::id().to_string())
            .arg("--timeout-seconds")
            .arg(self.timeout.as_secs().to_string())
            .process_group(0)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .context("无法启动 Harness SDK 监督进程")?;
        let input = serde_json::to_vec(&request)?;
        let mut stdin = child.stdin.take().context("无法打开桥接输入")?;
        let mut stdout = child.stdout.take().context("无法打开桥接输出")?;
        let mut stderr = child.stderr.take().context("无法打开桥接错误输出")?;
        // Drain both pipes while writing: large transcripts must not deadlock on
        // pipe capacity. The deadline also covers a child that never reads stdin.
        std::thread::scope(|scope| -> Result<Value> {
            let writer = scope.spawn(move || stdin.write_all(&input));
            let output = scope.spawn(move || {
                let mut bytes = Vec::new();
                stdout.read_to_end(&mut bytes).map(|_| bytes)
            });
            let errors = scope.spawn(move || {
                // Adapter diagnostics can include session data; drain without forwarding.
                std::io::copy(&mut stderr, &mut std::io::sink())
            });
            // The supervisor enforces the deadline and also survives hub death.
            let status = child.wait();
            let written = writer
                .join()
                .map_err(|_| anyhow::anyhow!("桥接输入线程失败"))?;
            let bytes = output
                .join()
                .map_err(|_| anyhow::anyhow!("桥接输出线程失败"))?;
            let drained = errors
                .join()
                .map_err(|_| anyhow::anyhow!("桥接错误线程失败"))?;
            let status = status?;
            ensure!(
                status.code() != Some(124),
                crate::error::fault(
                    "ADAPTER_TIMEOUT",
                    "DeepSeek SDK 执行超时",
                    "检查 Harness SDK 后，用同一计划或指派 ID 重试。",
                    true
                )
            );
            ensure!(
                status.success(),
                crate::error::fault(
                    "ADAPTER_UNAVAILABLE",
                    "DeepSeek SDK 操作未完成",
                    "检查 Harness SDK 安装、版本和会话库权限后，用同一指派 ID 重试。",
                    true
                )
            );
            written?;
            drained?;
            Ok(serde_json::from_slice(&bytes?)?)
        })
    }
    pub fn discover(&self) -> Result<Vec<Discovered<PathBuf>>> {
        let values = self.call(json!({"op":"discover"}))?;
        values
            .as_array()
            .context("Harness 返回了无效列表")?
            .iter()
            .map(|v| {
                Ok(Discovered {
                    meta: serde_json::from_value(v.clone())?,
                    reference: serde_json::from_value(v["reference"].clone())?,
                })
            })
            .collect()
    }
    pub fn read(&self, id: &str) -> Result<Transcript<Common>> {
        let value = self.call(json!({"op":"read","id":id}))?;
        Ok(Simple::to_common(&Simple::from_text(
            &serde_json::to_string(&value)?,
        )?)?)
    }
    pub fn validate(&self, id: &str, cwd: &Path, path: &Path) -> Result<()> {
        let value = self.call(json!({"op":"validate","id":id,"cwd":cwd,"path":path}))?;
        match value["issue"].as_str() {
            Some("ACCESS_DENIED") => {
                Err(std::io::Error::from(std::io::ErrorKind::PermissionDenied).into())
            }
            Some("TARGET_MISSING") => Err(crate::error::fault(
                "TARGET_MISSING",
                "已发布的目标会话已不存在",
                "恢复原目标会话后，用同一指派 ID 重试。",
                true,
            )),
            _ if value["valid"] == true => Ok(()),
            _ => Err(crate::error::target_invalid()),
        }
    }
    pub fn prepare(
        &self,
        common: &Transcript<Common>,
        staging: &Path,
    ) -> Result<(Vec<u8>, PathBuf, Transcript<Common>)> {
        let simple = Simple::to_text(&Simple::from_common(common)?)?;
        let value = self.call(json!({"op":"prepare","root":staging,"transcript":serde_json::from_str::<Value>(&simple)?}))?;
        let file: PathBuf = serde_json::from_value(value["path"].clone())?;
        let target = self.root.join(file.strip_prefix(staging)?);
        let delivered = Simple::to_common(&Simple::from_text(&serde_json::to_string(&value)?)?)?;
        Ok((fs::read(file)?, target, delivered))
    }
}
