//! Stable, account-owned group conversations; public context never enters personal memory.
use super::{Config, config, store};
use crate::{
    auth::User,
    db::{id, now},
    error::{Error, Result, invalid},
    state::App,
};
use axum::{
    Json,
    extract::{Path, State},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::SqlitePool;
use std::time::Duration;
use tokio_util::sync::CancellationToken;

#[derive(Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct GroupSettings {
    pub name: String,
    pub enabled: bool,
    pub profile_id: Option<String>,
    pub system_prompt: String,
    pub reply_mentioned: Option<bool>,
    pub collect_memory: Option<bool>,
}
impl Default for GroupSettings {
    fn default() -> Self {
        Self {
            name: String::new(),
            enabled: true,
            profile_id: None,
            system_prompt: String::new(),
            reply_mentioned: None,
            collect_memory: None,
        }
    }
}
impl GroupSettings {
    pub fn validate(&self) -> Result<()> {
        if self.name.chars().count() > 80 || self.system_prompt.chars().count() > 2000 {
            return Err(invalid("群名称最多80字，群人设最多2000字"));
        }
        Ok(())
    }
}
#[derive(Clone, Serialize, sqlx::FromRow)]
pub struct Session {
    pub id: String,
    pub group_id: String,
    pub created_at: i64,
    pub updated_at: i64,
}
pub async fn ensure(app: &App, user: &User, group: &str) -> Result<Session> {
    let pool = store::pool(app).await?;
    sqlx::query("INSERT INTO group_sessions(id,user_id,group_id,created_at,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id,group_id) DO NOTHING")
        .bind(id()).bind(&user.id).bind(group).bind(now()).bind(now()).execute(pool).await?;
    Ok(
        sqlx::query_as("SELECT * FROM group_sessions WHERE user_id=? AND group_id=?")
            .bind(&user.id)
            .bind(group)
            .fetch_one(pool)
            .await?,
    )
}
pub async fn touch(app: &App, user: &User, group: &str) -> Result<()> {
    ensure(app, user, group).await?;
    sqlx::query("UPDATE group_sessions SET updated_at=? WHERE user_id=? AND group_id=?")
        .bind(now())
        .bind(&user.id)
        .bind(group)
        .execute(store::pool(app).await?)
        .await?;
    Ok(())
}
pub async fn history(
    app: &App,
    user: &User,
    group: &str,
    before: i64,
    limit: usize,
) -> Result<Vec<store::Event>> {
    let mut history = store::history(app, user, group, None, false, before, limit as i64).await?;
    history.extend(store::history(app, user, group, None, true, before, limit as i64).await?);
    history.sort_by(|a, b| {
        (b.occurred_at, b.created_at, &b.id).cmp(&(a.occurred_at, a.created_at, &a.id))
    });
    history.truncate(limit);
    history.reverse();
    Ok(history)
}
fn ok(value: Value) -> Json<Value> {
    Json(json!({"ok":true,"data":value}))
}
pub fn reply_key(cfg: &Config, group: &str) -> String {
    crate::db::digest(&json!({"owner":cfg.owner_qq,"bot":cfg.bot_qq,"profile":cfg.profile_id,"onebot":cfg.onebot,"group":cfg.group_settings.get(group)}).to_string())
}
pub async fn list(State(app): State<App>, user: User) -> Result<Json<Value>> {
    let cfg = config(&app, &user).await?;
    let mut groups = vec![];
    for group in &cfg.groups {
        let session = ensure(&app, &user, group).await?;
        let settings = cfg.group_settings.get(group).cloned().unwrap_or_default();
        let effective = cfg.for_group(group).unwrap();
        let member: (i64, Option<i64>) = sqlx::query_as(
            "SELECT COUNT(*),MAX(occurred_at) FROM events WHERE user_id=? AND group_id=?",
        )
        .bind(&user.id)
        .bind(group)
        .fetch_one(store::pool(&app).await?)
        .await?;
        let owner: (i64, Option<i64>) = sqlx::query_as(
            "SELECT COUNT(*),MAX(occurred_at) FROM qq_owner_events WHERE user_id=? AND group_id=?",
        )
        .bind(&user.id)
        .bind(group)
        .fetch_one(&app.pool)
        .await?;
        groups.push(json!({"group_id":group,"settings":settings,"conversation":session,"messages":member.0+owner.0,"last_seen":member.1.max(owner.1),"active":effective.enabled,"profile_id":effective.profile_id,"reply_mentioned":effective.reply_mentioned,"collect_memory":effective.collect_memory}));
    }
    Ok(ok(json!(groups)))
}
pub async fn conversation(
    State(app): State<App>,
    user: User,
    Path(group): Path<String>,
) -> Result<Json<Value>> {
    let cfg = config(&app, &user).await?;
    if !cfg.groups.contains(&group) {
        return Err(Error::NotFound);
    }
    Ok(ok(
        json!({"conversation":ensure(&app,&user,&group).await?,"history":history(&app,&user,&group,now()+300,100).await?}),
    ))
}
// Compare the encrypted settings snapshot to avoid losing another simultaneous group edit.
async fn snapshot(app: &App, user: &User) -> Result<(Option<Value>, Value)> {
    let raw = crate::settings::get(app, user, "integrations").await?;
    let value = match &raw {
        Some(v) => serde_json::from_str(
            &app.secrets
                .decrypt(v.as_str().ok_or_else(|| invalid("接入配置损坏"))?)?,
        )
        .map_err(|_| invalid("接入配置损坏"))?,
        None => json!({}),
    };
    Ok((raw, value))
}
async fn commit(
    app: &App,
    user: &User,
    raw: Option<Value>,
    old: &Value,
    mut value: Value,
    cfg: Config,
) -> Result<()> {
    value["qq"] = json!(cfg);
    super::validate_config(app, user, &value["qq"], &old["qq"]).await?;
    if value.to_string().len() > 100_000 {
        return Err(invalid("接入配置过大"));
    }
    let encrypted = json!(app.secrets.encrypt(&value.to_string())?).to_string();
    let changed = if let Some(raw) = raw {
        sqlx::query(
            "UPDATE settings SET value=? WHERE user_id=? AND key='integrations' AND value=?",
        )
        .bind(encrypted)
        .bind(&user.id)
        .bind(raw.to_string())
        .execute(&app.pool)
        .await?
        .rows_affected()
    } else {
        sqlx::query("INSERT INTO settings(user_id,key,value) VALUES(?,'integrations',?) ON CONFLICT DO NOTHING")
            .bind(&user.id).bind(encrypted).execute(&app.pool).await?.rows_affected()
    };
    if changed == 0 {
        return Err(Error::Conflict("配置刚被修改，请刷新后重试".into()));
    }
    Ok(())
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Create {
    pub group_id: String,
    pub settings: GroupSettings,
}
pub async fn create(
    State(app): State<App>,
    user: User,
    Json(input): Json<Create>,
) -> Result<Json<Value>> {
    if !super::valid_id(&input.group_id) {
        return Err(invalid("请填写有效群号"));
    }
    let (raw, value) = snapshot(&app, &user).await?;
    let mut cfg: Config = serde_json::from_value(value.get("qq").cloned().unwrap_or(json!({})))
        .map_err(|_| invalid("QQ 配置无效"))?;
    if cfg.groups.contains(&input.group_id) {
        return Err(Error::Conflict("这个群已添加，每个群只对应一个会话".into()));
    }
    cfg.groups.push(input.group_id.clone());
    cfg.group_settings
        .insert(input.group_id.clone(), input.settings);
    commit(&app, &user, raw, &value, value.clone(), cfg).await?;
    Ok(ok(json!(ensure(&app, &user, &input.group_id).await?)))
}
pub async fn update(
    State(app): State<App>,
    user: User,
    Path(group): Path<String>,
    Json(settings): Json<GroupSettings>,
) -> Result<Json<Value>> {
    let (raw, value) = snapshot(&app, &user).await?;
    let mut cfg: Config = serde_json::from_value(value.get("qq").cloned().unwrap_or(json!({})))
        .map_err(|_| invalid("QQ 配置无效"))?;
    if !cfg.groups.contains(&group) {
        return Err(Error::NotFound);
    }
    cfg.group_settings.insert(group, settings);
    commit(&app, &user, raw, &value, value.clone(), cfg).await?;
    Ok(ok(json!({"saved":true})))
}
pub async fn remove(
    State(app): State<App>,
    user: User,
    Path(group): Path<String>,
) -> Result<Json<Value>> {
    let (raw, value) = snapshot(&app, &user).await?;
    let mut cfg: Config = serde_json::from_value(value.get("qq").cloned().unwrap_or(json!({})))
        .map_err(|_| invalid("QQ 配置无效"))?;
    if !cfg.groups.contains(&group) {
        return Err(Error::NotFound);
    }
    cfg.groups.retain(|g| g != &group);
    cfg.group_settings.remove(&group);
    if cfg.groups.is_empty() {
        cfg.enabled = false;
    }
    commit(&app, &user, raw, &value, value.clone(), cfg).await?;
    Ok(ok(json!({"removed":true,"history_retained":true})))
}

/// Serializes generation and delivery for one group across all worker processes.
pub struct Lease {
    pool: SqlitePool,
    sid: String,
    token: String,
    stop: CancellationToken,
    pub lost: CancellationToken,
}
impl Lease {
    pub async fn acquire(
        app: &App,
        user: &User,
        group: &str,
        cancel: &CancellationToken,
    ) -> Result<Self> {
        let session = ensure(app, user, group).await?;
        let pool = store::pool(app).await?.clone();
        let token = id();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(180);
        loop {
            if cancel.is_cancelled() || app.shutdown.is_cancelled() {
                return Err(Error::Cancelled);
            }
            let n = sqlx::query("UPDATE group_sessions SET lease_token=?,lease_until=? WHERE id=? AND (lease_token IS NULL OR lease_until<=?)")
                .bind(&token).bind(now()+90).bind(&session.id).bind(now()).execute(&pool).await?.rows_affected();
            if n == 1 {
                break;
            }
            tokio::select! {
                _=cancel.cancelled()=>return Err(Error::Cancelled),
                _=app.shutdown.cancelled()=>return Err(Error::Cancelled),
                _=tokio::time::sleep_until(deadline)=>return Err(Error::Busy),
                _=tokio::time::sleep(Duration::from_millis(100))=>(),
            }
        }
        let lease = Self {
            pool,
            sid: session.id,
            token,
            stop: CancellationToken::new(),
            lost: CancellationToken::new(),
        };
        let (pool, sid, token, stop, lost) = (
            lease.pool.clone(),
            lease.sid.clone(),
            lease.token.clone(),
            lease.stop.clone(),
            lease.lost.clone(),
        );
        tokio::spawn(async move {
            loop {
                tokio::select! {_=stop.cancelled()=>break,_=tokio::time::sleep(Duration::from_secs(20))=>()}
                let r=sqlx::query("UPDATE group_sessions SET lease_until=? WHERE id=? AND lease_token=? AND lease_until>?")
                    .bind(now()+90).bind(&sid).bind(&token).bind(now()).execute(&pool).await;
                if !r.is_ok_and(|r| r.rows_affected() == 1) {
                    lost.cancel();
                    break;
                }
            }
        });
        Ok(lease)
    }
    pub async fn check(&self) -> Result<()> {
        let valid:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM group_sessions WHERE id=? AND lease_token=? AND lease_until>?)")
            .bind(&self.sid).bind(&self.token).bind(now()).fetch_one(&self.pool).await?;
        if !valid || self.lost.is_cancelled() {
            return Err(Error::Conflict("群会话租约已失效".into()));
        }
        Ok(())
    }
}
impl Drop for Lease {
    fn drop(&mut self) {
        self.stop.cancel();
        let (pool, sid, token) = (self.pool.clone(), self.sid.clone(), self.token.clone());
        tokio::spawn(async move {
            let _=sqlx::query("UPDATE group_sessions SET lease_token=NULL,lease_until=NULL WHERE id=? AND lease_token=?").bind(sid).bind(token).execute(&pool).await;
        });
    }
}
