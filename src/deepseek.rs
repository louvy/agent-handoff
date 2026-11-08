use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};
use txcript::{Codec, Common, Discovered, TextCodec, Transcript, harness::simple::Simple};

#[derive(Clone)]
pub(crate) struct DeepSeek {
    pub root: PathBuf,
    package: PathBuf,
}
impl DeepSeek {
    pub fn new(root: Option<&Path>, package: Option<&Path>) -> Result<Self> {
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
        })
    }
    fn call(&self, mut request: Value) -> Result<Value> {
        if request.get("root").is_none() {
            request["root"] = json!(self.root);
        }
        let mut child = Command::new("node")
            .args([
                "--input-type=module",
                "-e",
                include_str!("../adapters/deepseek.mjs"),
            ])
            .arg(&self.package)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .context("无法启动 Harness SDK 桥接")?;
        let mut stdin = child.stdin.take().context("无法打开桥接输入")?;
        stdin.write_all(&serde_json::to_vec(&request)?)?;
        drop(stdin);
        let out = child.wait_with_output()?;
        ensure!(
            out.status.success(),
            crate::error::fault(
                "ADAPTER_UNAVAILABLE",
                "DeepSeek SDK 操作未完成",
                "检查 Harness SDK 安装、版本和会话库权限后，用同一指派 ID 重试。",
                true
            )
        );
        Ok(serde_json::from_slice(&out.stdout)?)
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
    ) -> Result<(Vec<u8>, PathBuf)> {
        let simple = Simple::to_text(&Simple::from_common(common)?)?;
        let value = self.call(json!({"op":"prepare","root":staging,"transcript":serde_json::from_str::<Value>(&simple)?}))?;
        let file: PathBuf = serde_json::from_value(value["path"].clone())?;
        let target = self.root.join(file.strip_prefix(staging)?);
        Ok((fs::read(file)?, target))
    }
}
