use crate::db::Secrets;
use std::{
    collections::HashMap,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::sync::Semaphore;
use tokio_util::sync::CancellationToken;

#[derive(Clone)]
pub struct App {
    pub pool: sqlx::SqlitePool,
    pub http: reqwest::Client,
    pub secrets: Secrets,
    pub data_dir: PathBuf,
    pub allow_private_network: bool,
    pub shutdown: CancellationToken,
    pub chats: Arc<Semaphore>,
    pub password_workers: Arc<Semaphore>,
    pub qq_pool: Arc<tokio::sync::OnceCell<sqlx::SqlitePool>>,
    login_limits: Arc<Mutex<HashMap<String, (Instant, u32)>>>,
}
impl App {
    pub fn new(
        pool: sqlx::SqlitePool,
        secrets: Secrets,
        data_dir: PathBuf,
        allow_private_network: bool,
    ) -> anyhow::Result<Self> {
        Ok(Self {
            pool,
            secrets,
            data_dir,
            allow_private_network,
            http: reqwest::Client::builder()
                .connect_timeout(Duration::from_secs(10))
                .timeout(Duration::from_secs(180))
                .redirect(reqwest::redirect::Policy::none())
                .build()?,
            shutdown: CancellationToken::new(),
            chats: Arc::new(Semaphore::new(32)),
            password_workers: Arc::new(Semaphore::new(4)),
            qq_pool: Arc::new(tokio::sync::OnceCell::new()),
            login_limits: Default::default(),
        })
    }
    pub fn allow_login(&self, key: &str) -> bool {
        let mut limits = self.login_limits.lock().unwrap_or_else(|e| e.into_inner());
        limits.retain(|_, (start, _)| start.elapsed() < Duration::from_secs(60));
        if limits.len() >= 10_000 && !limits.contains_key(key) {
            return false;
        }
        let item = limits.entry(key.to_owned()).or_insert((Instant::now(), 0));
        item.1 += 1;
        item.1 <= 10
    }
}
