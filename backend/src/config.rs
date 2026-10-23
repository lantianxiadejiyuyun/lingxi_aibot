use clap::{Parser, Subcommand};
use std::{net::SocketAddr, path::PathBuf};

#[derive(Parser)]
#[command(version, about = "灵犀 Rust 后端")]
pub struct Cli {
    #[arg(
        long,
        env = "LINGXI_DATA_DIR",
        default_value = "data/rust",
        global = true
    )]
    pub data_dir: PathBuf,
    #[arg(
        long,
        env = "LINGXI_BIND",
        default_value = "127.0.0.1:8000",
        global = true
    )]
    pub bind: SocketAddr,
    #[arg(
        long,
        env = "LINGXI_ALLOW_PRIVATE_NETWORK",
        default_value = "false",
        global = true
    )]
    pub allow_private_network: bool,
    #[arg(
        long,
        env = "LINGXI_FEISHU_EMBEDDED",
        default_value = "false",
        global = true
    )]
    pub feishu_embedded: bool,
    #[arg(
        long,
        env = "LINGXI_FEISHU_NODE",
        default_value = "/usr/local/bin/node",
        global = true
    )]
    pub feishu_node: PathBuf,
    #[arg(
        long,
        env = "LINGXI_FEISHU_SCRIPT",
        default_value = "/opt/lingxi/feishu/bridge.mjs",
        global = true
    )]
    pub feishu_script: PathBuf,
    #[arg(long, env = "LINGXI_PUBLIC_URL", global = true)]
    pub public_url: Option<String>,
    #[arg(long, env = "LINGXI_NAPCAT_WEBUI_URL", global = true)]
    pub napcat_webui_url: Option<String>,
    #[arg(long, env = "LINGXI_NAPCAT_HTTP_URL", global = true)]
    pub napcat_http_url: Option<String>,
    #[arg(
        long,
        env = "LINGXI_NAPCAT_MANAGED",
        default_value = "false",
        global = true
    )]
    pub napcat_managed: bool,
    #[arg(
        long,
        env = "LINGXI_NAPCAT_HTTP_TOKEN",
        hide_env_values = true,
        global = true
    )]
    pub napcat_http_token: Option<String>,
    #[command(subcommand)]
    pub command: Option<Command>,
}
#[derive(Subcommand)]
pub enum Command {
    /// HTTP server and optional embedded background worker.
    Serve {
        #[arg(long)]
        no_worker: bool,
    },
    /// Run only the durable task worker (can share the same local SQLite file).
    Worker,
    /// Create the first administrator. Password is read from the environment.
    Init {
        #[arg(long, default_value = "admin")]
        username: String,
        #[arg(long, env = "LINGXI_ADMIN_PASSWORD", hide_env_values = true)]
        password: String,
    },
    /// Reset a local user's password and invalidate all their tokens.
    ResetPassword {
        #[arg(long)]
        username: String,
        #[arg(long, env = "LINGXI_ADMIN_PASSWORD", hide_env_values = true)]
        password: String,
    },
    /// Consistent SQLite backup; store master.key separately with the backup.
    Backup { path: PathBuf },
    /// Import a legacy export into an empty database, with a new initial password.
    Import {
        path: PathBuf,
        #[arg(long, env = "LINGXI_IMPORT_PASSWORD", hide_env_values = true)]
        password: String,
    },
}
