//! The official Feishu SDK runs as a supervised child of the web service.
//! Each account receives a narrowly scoped, ephemeral credential and durable inbox.
use crate::{db::digest, state::App};
use std::{
    collections::{HashMap, HashSet},
    path::PathBuf,
    process::Stdio,
    time::Duration,
};
use tokio::{process::Child, time::Instant};

pub struct RuntimeConfig {
    pub base_url: String,
    pub node: PathBuf,
    pub script: PathBuf,
}

impl RuntimeConfig {
    pub fn validate(&self) -> anyhow::Result<()> {
        let url = url::Url::parse(&self.base_url)?;
        anyhow::ensure!(
            url.scheme() == "http"
                && url.username().is_empty()
                && url.password().is_none()
                && url.query().is_none()
                && url.fragment().is_none()
                && url.path() == "/"
                && url.host_str().is_some_and(|host| {
                    host == "localhost"
                        || host
                            .trim_matches(['[', ']'])
                            .parse::<std::net::IpAddr>()
                            .is_ok()
                }),
            "embedded Feishu must use the configured HTTP listener address"
        );
        anyhow::ensure!(
            self.node.is_file(),
            "embedded Feishu Node runtime is missing"
        );
        anyhow::ensure!(
            self.script.is_file(),
            "embedded Feishu bridge script is missing"
        );
        Ok(())
    }
}

struct Process {
    child: Child,
    token_hash: String,
    started: Instant,
}

struct Account {
    process: Option<Process>,
    failures: u32,
    retry_at: Instant,
}

impl Default for Account {
    fn default() -> Self {
        Self {
            process: None,
            failures: 0,
            retry_at: Instant::now(),
        }
    }
}

fn retry_delay(failures: u32) -> Duration {
    Duration::from_secs((2u64 << failures.min(6)).min(120))
}

/// User IDs are never used as path components, including imported legacy IDs.
pub fn account_dir(app: &App, user_id: &str) -> PathBuf {
    app.data_dir.join("feishu").join(digest(user_id))
}

async fn start(app: &App, cfg: &RuntimeConfig, user_id: &str) -> anyhow::Result<Process> {
    let dir = account_dir(app, user_id);
    let inbox = dir.join("inbox");
    tokio::fs::create_dir_all(&inbox).await?;
    match tokio::fs::remove_file(dir.join("health")).await {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        for path in [&dir, &inbox] {
            tokio::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).await?;
        }
    }
    let token = format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    );
    let token_hash = digest(&token);
    let changed = sqlx::query(
        "UPDATE feishu_state SET managed_token_hash=?,connection_state='connecting',heartbeat_at=? WHERE user_id=? AND enabled=1 AND EXISTS(SELECT 1 FROM users WHERE id=? AND disabled=0)",
    )
    .bind(&token_hash)
    .bind(crate::db::now())
    .bind(user_id)
    .bind(user_id)
    .execute(&app.pool)
    .await?;
    anyhow::ensure!(
        changed.rows_affected() == 1,
        "Feishu account is no longer enabled"
    );

    // No credentials in argv or child output. Do not give the SDK the parent's
    // database/Qdrant credentials; it only needs its account-scoped bridge token.
    let spawn = tokio::process::Command::new(&cfg.node)
        .arg(&cfg.script)
        .env_clear()
        .env("NODE_ENV", "production")
        .env("LINGXI_URL", &cfg.base_url)
        .env("LINGXI_FEISHU_BRIDGE_TOKEN", token)
        .env("LINGXI_FEISHU_SPOOL", inbox)
        .env("LINGXI_FEISHU_HEALTH", dir.join("health"))
        .env("LINGXI_FEISHU_MANAGED", "true")
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn();
    match spawn {
        Ok(child) => Ok(Process {
            child,
            token_hash,
            started: Instant::now(),
        }),
        Err(error) => {
            revoke(app, user_id, &token_hash, "failed").await;
            Err(error.into())
        }
    }
}

async fn revoke(app: &App, user_id: &str, token_hash: &str, state: &str) {
    let heartbeat = (state == "failed").then(crate::db::now);
    let _ = sqlx::query("UPDATE feishu_state SET managed_token_hash=NULL,connection_state=?,heartbeat_at=? WHERE user_id=? AND managed_token_hash=?")
        .bind(state).bind(heartbeat).bind(user_id).bind(token_hash).execute(&app.pool).await;
}

async fn stop(app: &App, user_id: &str, mut process: Process, state: &str) {
    // EOF is a private graceful-shutdown signal, avoiding platform-specific kill
    // commands. The Node handler closes the SDK before exiting its event loop.
    drop(process.child.stdin.take());
    if tokio::time::timeout(Duration::from_secs(6), process.child.wait())
        .await
        .is_err()
    {
        let _ = process.child.kill().await;
    }
    revoke(app, user_id, &process.token_hash, state).await;
}

async fn health_stale(app: &App, user_id: &str, process: &Process) -> bool {
    if process.started.elapsed() < Duration::from_secs(90) {
        return false;
    }
    tokio::fs::metadata(account_dir(app, user_id).join("health"))
        .await
        .ok()
        .and_then(|metadata| metadata.modified().ok())
        .and_then(|modified| modified.elapsed().ok())
        .is_none_or(|age| age > Duration::from_secs(90))
}

async fn reconcile(
    app: &App,
    cfg: &RuntimeConfig,
    accounts: &mut HashMap<String, Account>,
) -> anyhow::Result<()> {
    let enabled: HashSet<String> = sqlx::query_scalar::<_, String>(
        "SELECT f.user_id FROM feishu_state f JOIN users u ON u.id=f.user_id WHERE f.enabled=1 AND u.disabled=0",
    )
    .fetch_all(&app.pool)
    .await?
    .into_iter()
    .collect();
    let removed: Vec<String> = accounts
        .keys()
        .filter(|id| !enabled.contains(*id))
        .cloned()
        .collect();
    for user_id in removed {
        if let Some(mut account) = accounts.remove(&user_id) {
            if let Some(process) = account.process.take() {
                stop(app, &user_id, process, "disabled").await;
            }
        }
    }
    for user_id in enabled {
        let account = accounts.entry(user_id.clone()).or_default();
        let exited = match account.process.as_mut() {
            Some(process) => process.child.try_wait()?.is_some(),
            None => false,
        };
        let stalled = match account.process.as_ref() {
            Some(process) if !exited => health_stale(app, &user_id, process).await,
            _ => false,
        };
        if exited || stalled {
            let process = account.process.take().expect("checked child");
            if process.started.elapsed() > Duration::from_secs(120) {
                account.failures = 0;
            }
            stop(app, &user_id, process, "failed").await;
            account.failures = account.failures.saturating_add(1);
            account.retry_at = Instant::now() + retry_delay(account.failures);
            tracing::warn!(
                user_id,
                stalled,
                "embedded Feishu SDK stopped; restart scheduled"
            );
        }
        if account.process.is_none()
            && Instant::now() >= account.retry_at
            && !app.shutdown.is_cancelled()
        {
            match start(app, cfg, &user_id).await {
                Ok(process) => {
                    account.process = Some(process);
                    tracing::info!(user_id, "embedded Feishu SDK started");
                }
                Err(_) => {
                    account.failures = account.failures.saturating_add(1);
                    account.retry_at = Instant::now() + retry_delay(account.failures);
                    tracing::warn!(
                        user_id,
                        "embedded Feishu SDK could not start; retry scheduled"
                    );
                }
            }
        }
    }
    Ok(())
}

async fn stop_all(app: &App, accounts: &mut HashMap<String, Account>) {
    let mut stops = tokio::task::JoinSet::new();
    for (user_id, account) in accounts.drain() {
        if let Some(process) = account.process {
            let app = app.clone();
            stops.spawn(async move { stop(&app, &user_id, process, "offline").await });
        }
    }
    while stops.join_next().await.is_some() {}
}

/// Run once for the serving process. Worker-only processes must not start it.
pub async fn run(app: App, cfg: RuntimeConfig) {
    if !app.feishu_embedded {
        return;
    }
    if cfg.validate().is_err() {
        let _ = sqlx::query("UPDATE feishu_state SET managed_token_hash=NULL,connection_state='failed',heartbeat_at=? WHERE enabled=1")
            .bind(crate::db::now()).execute(&app.pool).await;
        tracing::error!("embedded Feishu runtime configuration is invalid");
        return;
    }
    // Tokens are only valid for one parent lifetime. Never retain a credential
    // from a previously crashed process, even for a now-disabled account.
    let _ = sqlx::query("UPDATE feishu_state SET managed_token_hash=NULL")
        .execute(&app.pool)
        .await;
    let mut accounts = HashMap::new();
    let mut last_sync = Instant::now();
    loop {
        if app.shutdown.is_cancelled() {
            break;
        }
        if reconcile(&app, &cfg, &mut accounts).await.is_ok() {
            last_sync = Instant::now();
        } else {
            tracing::warn!("embedded Feishu account reconciliation failed");
            // If account status cannot be checked, do not keep receiving forever.
            if last_sync.elapsed() > Duration::from_secs(45) {
                stop_all(&app, &mut accounts).await;
            }
        }
        tokio::select! {
            _ = app.shutdown.cancelled() => break,
            _ = tokio::time::sleep(Duration::from_secs(3)) => {}
        }
    }
    stop_all(&app, &mut accounts).await;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn restart_backoff_is_bounded() {
        assert_eq!(retry_delay(1), Duration::from_secs(4));
        assert_eq!(retry_delay(3), Duration::from_secs(16));
        assert_eq!(retry_delay(u32::MAX), Duration::from_secs(120));
    }

    #[tokio::test]
    async fn missing_runtime_is_visible_and_does_not_change_external_tokens() {
        let dir = tempfile::tempdir().unwrap();
        let mut app = App::new(
            crate::db::memory().await.unwrap(),
            crate::db::Secrets::from_key(&[7; 32]),
            dir.path().into(),
            false,
        )
        .unwrap();
        app.feishu_embedded = true;
        sqlx::query("INSERT INTO users(id,username,password_hash,created_at) VALUES('owner','owner','unused',0)").execute(&app.pool).await.unwrap();
        sqlx::query("INSERT INTO feishu_state(user_id,enabled,token_hash,managed_token_hash) VALUES('owner',1,'external','old-managed')").execute(&app.pool).await.unwrap();
        run(
            app.clone(),
            RuntimeConfig {
                base_url: "http://127.0.0.1:8000".into(),
                node: dir.path().join("missing-node"),
                script: dir.path().join("missing-script"),
            },
        )
        .await;
        let state: (String, String, Option<String>) = sqlx::query_as(
            "SELECT connection_state,token_hash,managed_token_hash FROM feishu_state",
        )
        .fetch_one(&app.pool)
        .await
        .unwrap();
        assert_eq!(state, ("failed".into(), "external".into(), None));
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn supervisor_preserves_external_credentials_restarts_and_stops_disabled_accounts() {
        let dir = tempfile::tempdir().unwrap();
        let pool = crate::db::memory().await.unwrap();
        let mut app = App::new(
            pool,
            crate::db::Secrets::from_key(&[7; 32]),
            dir.path().into(),
            false,
        )
        .unwrap();
        app.feishu_embedded = true;
        for id in ["first", "second", "../../imported-id"] {
            sqlx::query(
                "INSERT INTO users(id,username,password_hash,created_at) VALUES(?,?, 'unused',0)",
            )
            .bind(id)
            .bind(id)
            .execute(&app.pool)
            .await
            .unwrap();
            sqlx::query("INSERT INTO feishu_state(user_id,enabled,token_hash) VALUES(?,1,?)")
                .bind(id)
                .bind(format!("external-{id}"))
                .execute(&app.pool)
                .await
                .unwrap();
        }
        let script = dir.path().join("fake-bridge.sh");
        tokio::fs::write(&script, "#!/bin/sh\nread -r line\n")
            .await
            .unwrap();
        let cfg = RuntimeConfig {
            base_url: "http://127.0.0.1:8000".into(),
            node: "/bin/sh".into(),
            script,
        };
        cfg.validate().unwrap();
        let mut accounts = HashMap::new();
        reconcile(&app, &cfg, &mut accounts).await.unwrap();
        assert_eq!(accounts.len(), 3);
        let first_pid = accounts["first"].process.as_ref().unwrap().child.id();
        let hashes: Vec<String> = sqlx::query_scalar("SELECT managed_token_hash FROM feishu_state")
            .fetch_all(&app.pool)
            .await
            .unwrap();
        assert_eq!(hashes.iter().collect::<HashSet<_>>().len(), 3);
        assert!(hashes.iter().all(|hash| hash.len() == 64));
        assert!(account_dir(&app, "../../imported-id").starts_with(dir.path().join("feishu")));

        accounts
            .get_mut("first")
            .unwrap()
            .process
            .as_mut()
            .unwrap()
            .child
            .kill()
            .await
            .unwrap();
        reconcile(&app, &cfg, &mut accounts).await.unwrap();
        assert!(accounts["first"].process.is_none());
        accounts.get_mut("first").unwrap().retry_at = Instant::now();
        reconcile(&app, &cfg, &mut accounts).await.unwrap();
        assert_ne!(
            accounts["first"].process.as_ref().unwrap().child.id(),
            first_pid
        );

        // A child can be alive while its event loop stops progressing.
        accounts
            .get_mut("second")
            .unwrap()
            .process
            .as_mut()
            .unwrap()
            .started = Instant::now() - Duration::from_secs(91);
        reconcile(&app, &cfg, &mut accounts).await.unwrap();
        assert!(accounts["second"].process.is_none());
        assert!(accounts["second"].retry_at > Instant::now());

        sqlx::query("UPDATE users SET disabled=1 WHERE id='first'")
            .execute(&app.pool)
            .await
            .unwrap();
        sqlx::query("UPDATE feishu_state SET enabled=0 WHERE user_id='second'")
            .execute(&app.pool)
            .await
            .unwrap();
        reconcile(&app, &cfg, &mut accounts).await.unwrap();
        assert_eq!(accounts.len(), 1);
        let count: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM feishu_state WHERE managed_token_hash IS NOT NULL",
        )
        .fetch_one(&app.pool)
        .await
        .unwrap();
        assert_eq!(count, 1);
        sqlx::query("DELETE FROM users WHERE id='../../imported-id'")
            .execute(&app.pool)
            .await
            .unwrap();
        reconcile(&app, &cfg, &mut accounts).await.unwrap();
        assert!(accounts.is_empty());

        sqlx::query("UPDATE users SET disabled=0 WHERE id='first'")
            .execute(&app.pool)
            .await
            .unwrap();
        reconcile(&app, &cfg, &mut accounts).await.unwrap();
        stop_all(&app, &mut accounts).await;
        assert!(accounts.is_empty());
        let rows: Vec<(String, Option<String>)> =
            sqlx::query_as("SELECT token_hash,managed_token_hash FROM feishu_state")
                .fetch_all(&app.pool)
                .await
                .unwrap();
        assert!(
            rows.iter()
                .all(|(external, managed)| external.starts_with("external-") && managed.is_none())
        );

        // Exercise the public lifecycle: server cancellation closes every child
        // and revokes only managed credentials before the database is closed.
        let shutdown = app.shutdown.clone();
        let running = tokio::spawn(run(app.clone(), cfg));
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                let active: bool = sqlx::query_scalar(
                    "SELECT managed_token_hash IS NOT NULL FROM feishu_state WHERE user_id='first'",
                )
                .fetch_one(&app.pool)
                .await
                .unwrap();
                if active {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        shutdown.cancel();
        tokio::time::timeout(Duration::from_secs(2), running)
            .await
            .unwrap()
            .unwrap();
        let active: bool = sqlx::query_scalar(
            "SELECT managed_token_hash IS NOT NULL FROM feishu_state WHERE user_id='first'",
        )
        .fetch_one(&app.pool)
        .await
        .unwrap();
        assert!(!active);
    }
}
