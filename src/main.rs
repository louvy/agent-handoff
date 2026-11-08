mod agent;
mod app;
mod assignment;
mod deepseek;
mod error;
mod maintenance;
mod mcp;
mod protocol;
use clap::Parser;
#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let cli = match app::Cli::try_parse() {
        Ok(cli) => cli,
        Err(error)
            if matches!(
                error.kind(),
                clap::error::ErrorKind::DisplayHelp | clap::error::ErrorKind::DisplayVersion
            ) =>
        {
            error.print()?;
            return Ok(());
        }
        Err(_) => {
            eprintln!(
                "{}",
                crate::error::response(&anyhow::anyhow!("不支持的输入参数"))
            );
            std::process::exit(2);
        }
    };
    if let app::Action::AdapterSupervisor {
        package,
        owner_pid,
        timeout_seconds,
    } = &cli.command
    {
        // Keep supervision independent of application state and SQLite locks.
        std::process::exit(deepseek::supervise(package, *owner_pid, *timeout_seconds).unwrap_or(1));
    }
    if matches!(cli.command, app::Action::Mcp) {
        if let Err(error) = mcp::serve(cli).await {
            eprintln!("{}", crate::error::response(&error));
            std::process::exit(1);
        }
        Ok(())
    } else {
        match app::run(cli) {
            Ok(value) => {
                println!("{}", serde_json::to_string_pretty(&value)?);
                Ok(())
            }
            Err(err) => {
                eprintln!("{}", crate::error::response(&err));
                std::process::exit(1);
            }
        }
    }
}
