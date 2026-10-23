use clap::Parser;
use lingxi::{
    config::{Cli, Command},
    db,
    state::App,
};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "lingxi=info,tower_http=info".into()),
        )
        .json()
        .init();
    let cli = Cli::parse();
    let pool = db::connect(&cli.data_dir.join("lingxi.sqlite3")).await?;
    let secrets = db::Secrets::load(&cli.data_dir.join("master.key"))?;
    let deployment = lingxi::deployment::Config::from_cli(&cli)?;
    let mut app = App::new(pool, secrets, cli.data_dir, cli.allow_private_network)?;
    app.feishu_embedded = cli.feishu_embedded;
    app.deployment = deployment;
    match cli.command.unwrap_or(Command::Serve { no_worker: false }) {
        Command::Init { username, password } => {
            let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM users")
                .fetch_one(&app.pool)
                .await?;
            anyhow::ensure!(
                count == 0,
                "database already initialized; create more users through the admin API"
            );
            let user = lingxi::auth::create_user(&app, &username, password, true).await?;
            for (name, action, cron) in [
                ("每日记忆整理", "consolidate", "0 4 * * *"),
                ("到期提醒", "reminders", "*/5 * * * *"),
            ] {
                lingxi::jobs::add_schedule(
                    &app,
                    &user,
                    lingxi::jobs::ScheduleInput {
                        name: name.into(),
                        action: action.into(),
                        params: serde_json::json!({}),
                        cron: cron.into(),
                        timezone: user.timezone.clone(),
                        enabled: true,
                    },
                )
                .await?;
            }
            println!("Administrator created: {}", user.username);
        }
        Command::ResetPassword { username, password } => {
            let hash = lingxi::auth::hash_password(&app, password).await?;
            let mut tx = app.pool.begin().await?;
            let uid: Option<String> = sqlx::query_scalar("SELECT id FROM users WHERE username=?")
                .bind(username)
                .fetch_optional(&mut *tx)
                .await?;
            let uid = uid.ok_or_else(|| anyhow::anyhow!("user not found"))?;
            sqlx::query("UPDATE users SET password_hash=?,disabled=0 WHERE id=?")
                .bind(hash)
                .bind(&uid)
                .execute(&mut *tx)
                .await?;
            sqlx::query("DELETE FROM tokens WHERE user_id=?")
                .bind(uid)
                .execute(&mut *tx)
                .await?;
            tx.commit().await?;
            println!("Password updated; prior tokens revoked.");
        }
        Command::Backup { path } => {
            anyhow::ensure!(!path.exists(), "backup destination already exists");
            let qq_path = path.with_file_name(format!(
                "{}.qq.sqlite3",
                path.file_name().unwrap_or_default().to_string_lossy()
            ));
            let has_qq = app.data_dir.join("qq/people.sqlite3").exists();
            anyhow::ensure!(
                !has_qq || !qq_path.exists(),
                "QQ backup destination already exists"
            );
            sqlx::query("VACUUM INTO ?")
                .bind(path.to_string_lossy().as_ref())
                .execute(&app.pool)
                .await?;
            if has_qq {
                sqlx::query("VACUUM INTO ?")
                    .bind(qq_path.to_string_lossy().as_ref())
                    .execute(lingxi::qq::store::pool(&app).await?)
                    .await?;
                println!("QQ member metadata backup: {}", qq_path.display());
            }
            println!(
                "Database snapshots created. Keep master.key and assets with the backup. Back up Qdrant separately or rebuild its index."
            );
        }
        Command::Import { path, password } => {
            anyhow::ensure!(
                tokio::fs::metadata(&path).await?.len() <= 1_000_000_000,
                "snapshot exceeds 1 GB"
            );
            let bytes = tokio::fs::read(path).await?;
            println!(
                "{}",
                serde_json::to_string_pretty(
                    &lingxi::migration::import(&app, &bytes, password).await?
                )?
            );
        }
        Command::Worker => {
            let shutdown = app.shutdown.clone();
            tokio::spawn(async move {
                shutdown_signal().await;
                shutdown.cancel();
            });
            lingxi::jobs::worker(app.clone()).await;
        }
        Command::Serve { no_worker } => {
            let listener = tokio::net::TcpListener::bind(cli.bind).await?;
            let feishu = if app.feishu_embedded {
                let bound = listener.local_addr()?;
                let host = if bound.ip().is_unspecified() {
                    if bound.is_ipv4() {
                        "127.0.0.1".into()
                    } else {
                        "[::1]".into()
                    }
                } else if bound.is_ipv4() {
                    bound.ip().to_string()
                } else {
                    format!("[{}]", bound.ip())
                };
                Some(tokio::spawn(lingxi::feishu_runtime::run(
                    app.clone(),
                    lingxi::feishu_runtime::RuntimeConfig {
                        base_url: format!("http://{host}:{}", bound.port()),
                        node: cli.feishu_node,
                        script: cli.feishu_script,
                    },
                )))
            } else {
                None
            };
            let worker = if no_worker {
                None
            } else {
                Some(tokio::spawn(lingxi::jobs::worker(app.clone())))
            };
            let shutdown = app.shutdown.clone();
            tracing::info!(address=%cli.bind,"Lingxi Rust server ready");
            let server = axum::serve(listener, lingxi::api::router(app.clone()))
                .with_graceful_shutdown(async move {
                    shutdown_signal().await;
                    shutdown.cancel();
                })
                .await;
            app.shutdown.cancel();
            if let Some(feishu) = feishu {
                feishu.await?;
            }
            if let Some(worker) = worker {
                worker.await?;
            }
            server?;
        }
    }
    app.pool.close().await;
    Ok(())
}
async fn shutdown_signal() {
    #[cfg(unix)]
    {
        let mut signal = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("install SIGTERM handler");
        tokio::select! {_=tokio::signal::ctrl_c()=>(),_=signal.recv()=>()}
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}
