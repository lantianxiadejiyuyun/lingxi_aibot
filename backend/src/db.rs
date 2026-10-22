use crate::error::{Error, Result};
use aes_gcm::{
    Aes256Gcm, KeyInit,
    aead::{Aead, OsRng, rand_core::RngCore},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use sqlx::{
    SqlitePool,
    sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions},
};
use std::{path::Path, str::FromStr, time::Duration};

pub fn now() -> i64 {
    chrono::Utc::now().timestamp()
}
pub fn id() -> String {
    uuid::Uuid::new_v4().to_string()
}
pub fn digest(text: &str) -> String {
    use sha2::{Digest, Sha256};
    format!("{:x}", Sha256::digest(text.as_bytes()))
}
pub async fn connect(path: &Path) -> anyhow::Result<SqlitePool> {
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    let opts = SqliteConnectOptions::new()
        .filename(path)
        .create_if_missing(true)
        .foreign_keys(true)
        .journal_mode(SqliteJournalMode::Wal)
        .busy_timeout(Duration::from_secs(10));
    let pool = SqlitePoolOptions::new()
        .max_connections(8)
        .connect_with(opts)
        .await?;
    migrate(&pool).await?;
    Ok(pool)
}
pub async fn memory() -> anyhow::Result<SqlitePool> {
    let opts = SqliteConnectOptions::from_str("sqlite::memory:")?.foreign_keys(true);
    let pool = SqlitePoolOptions::new()
        .max_connections(1)
        .connect_with(opts)
        .await?;
    migrate(&pool).await?;
    Ok(pool)
}
async fn migrate(pool: &SqlitePool) -> anyhow::Result<()> {
    sqlx::migrate!("./migrations").run(pool).await?;
    Ok(())
}

/// Credentials remain encrypted at rest. This key is never returned through HTTP.
#[derive(Clone)]
pub struct Secrets(Aes256Gcm);
impl Secrets {
    pub fn from_key(key: &[u8; 32]) -> Self {
        Self(Aes256Gcm::new(key.into()))
    }
    pub fn load(path: &Path) -> anyhow::Result<Self> {
        if !path.exists() {
            let mut key = [0u8; 32];
            OsRng.fill_bytes(&mut key);
            let mut options = std::fs::OpenOptions::new();
            options.write(true).create_new(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.mode(0o600);
            }
            match options.open(path) {
                Ok(mut file) => {
                    use std::io::Write;
                    file.write_all(&key)?;
                    file.sync_all()?;
                }
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => (),
                Err(e) => return Err(e.into()),
            }
        }
        let key: [u8; 32] = std::fs::read(path)?
            .try_into()
            .map_err(|_| anyhow::anyhow!("invalid master.key"))?;
        Ok(Self::from_key(&key))
    }
    pub fn encrypt(&self, text: &str) -> Result<String> {
        let mut nonce = [0u8; 12];
        OsRng.fill_bytes(&mut nonce);
        let encrypted = self
            .0
            .encrypt((&nonce).into(), text.as_bytes())
            .map_err(|_| Error::Internal(anyhow::anyhow!("encryption failed")))?;
        let mut bytes = nonce.to_vec();
        bytes.extend(encrypted);
        Ok(STANDARD.encode(bytes))
    }
    pub fn decrypt(&self, text: &str) -> Result<String> {
        let bytes = STANDARD
            .decode(text)
            .map_err(|e| Error::Internal(e.into()))?;
        if bytes.len() < 12 {
            return Err(Error::Internal(anyhow::anyhow!("invalid ciphertext")));
        }
        let value = self
            .0
            .decrypt(bytes[..12].into(), &bytes[12..])
            .map_err(|_| {
                Error::Internal(anyhow::anyhow!(
                    "cannot decrypt credentials: check master.key"
                ))
            })?;
        String::from_utf8(value).map_err(|e| Error::Internal(e.into()))
    }
}
