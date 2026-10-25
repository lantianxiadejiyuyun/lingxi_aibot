use crate::{
    auth::User,
    db::{digest, id, now},
    error::{Error, Result, invalid},
    state::App,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::{
    SqlitePool,
    sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions},
};
use std::time::Duration;

pub async fn pool(app: &App) -> Result<&SqlitePool> {
    app.qq_pool
        .get_or_try_init(|| async {
            let directory = app.data_dir.join("qq");
            tokio::fs::create_dir_all(&directory)
                .await
                .map_err(|e| Error::Internal(e.into()))?;
            let opts = SqliteConnectOptions::new()
                .filename(directory.join("people.sqlite3"))
                .create_if_missing(true)
                .journal_mode(SqliteJournalMode::Wal)
                .busy_timeout(Duration::from_secs(10));
            let pool = SqlitePoolOptions::new()
                .max_connections(4)
                .connect_with(opts)
                .await?;
            sqlx::raw_sql(include_str!("../../qq_schema.sql"))
                .execute(&pool)
                .await?;
            sqlx::raw_sql(include_str!("../../mind_schema.sql"))
                .execute(&pool)
                .await?;
            Ok::<_, Error>(pool)
        })
        .await
}
#[derive(Clone, Serialize, sqlx::FromRow)]
pub struct Event {
    pub id: String,
    pub user_id: String,
    pub group_id: String,
    pub qq_id: String,
    pub name: String,
    pub content: String,
    pub triggered: bool,
    pub occurred_at: i64,
    pub created_at: i64,
    pub reply: String,
    pub delivery_status: String,
}
pub async fn event(app: &App, user: &User, event_id: &str, owner: bool) -> Result<Event> {
    let pool = if owner { &app.pool } else { pool(app).await? };
    let table = if owner { "qq_owner_events" } else { "events" };
    sqlx::query_as(&format!("SELECT * FROM {table} WHERE id=? AND user_id=?"))
        .bind(event_id)
        .bind(&user.id)
        .fetch_optional(pool)
        .await?
        .ok_or(Error::NotFound)
}
pub async fn insert(app: &App, user: &User, event: &Event, owner: bool) -> Result<()> {
    if owner {
        let mut tx = app.pool.begin().await?;
        let count = sqlx::query("INSERT INTO qq_owner_events(id,user_id,group_id,qq_id,name,content,triggered,occurred_at,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING")
            .bind(&event.id).bind(&user.id).bind(&event.group_id).bind(&event.qq_id).bind(&event.name).bind(&event.content).bind(event.triggered).bind(event.occurred_at).bind(now()).execute(&mut *tx).await?.rows_affected();
        if count > 0 {
            let cid: Option<String> = sqlx::query_scalar("SELECT conversation_id FROM channel_bindings WHERE user_id=? AND channel='qq_owner' AND external_id=?").bind(&user.id).bind(&event.group_id).fetch_optional(&mut *tx).await?;
            let cid = if let Some(cid) = cid {
                cid
            } else {
                let cid = id();
                sqlx::query("INSERT INTO conversations(id,user_id,title,created_at,updated_at) VALUES(?,?,?,?,?)").bind(&cid).bind(&user.id).bind(format!("QQ 主人 · 群 {}", event.group_id)).bind(now()).bind(now()).execute(&mut *tx).await?;
                sqlx::query("INSERT INTO channel_bindings(user_id,channel,external_id,conversation_id) VALUES(?,'qq_owner',?,?)").bind(&user.id).bind(&event.group_id).bind(&cid).execute(&mut *tx).await?;
                cid
            };
            let mid = sqlx::query("INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,'user',?,?)").bind(&cid).bind(&event.content).bind(event.occurred_at).execute(&mut *tx).await?.last_insert_rowid();
            sqlx::query("UPDATE qq_owner_events SET conversation_id=?,message_id=? WHERE id=?")
                .bind(&cid)
                .bind(mid)
                .bind(&event.id)
                .execute(&mut *tx)
                .await?;
            sqlx::query("UPDATE conversations SET updated_at=? WHERE id=?")
                .bind(now())
                .bind(cid)
                .execute(&mut *tx)
                .await?;
        }
        tx.commit().await?;
    } else {
        sqlx::query("INSERT INTO events(id,user_id,group_id,qq_id,name,content,triggered,occurred_at,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING")
            .bind(&event.id).bind(&user.id).bind(&event.group_id).bind(&event.qq_id).bind(&event.name).bind(&event.content).bind(event.triggered).bind(event.occurred_at).bind(now()).execute(pool(app).await?).await?;
    }
    Ok(())
}
pub async fn history(
    app: &App,
    user: &User,
    group: &str,
    qq: Option<&str>,
    owner: bool,
    before: i64,
    limit: i64,
) -> Result<Vec<Event>> {
    let table = if owner { "qq_owner_events" } else { "events" };
    let pool = if owner { &app.pool } else { pool(app).await? };
    Ok(sqlx::query_as(&format!("SELECT * FROM {table} WHERE user_id=? AND group_id=? AND (? IS NULL OR qq_id=?) AND occurred_at<=? ORDER BY occurred_at DESC,id DESC LIMIT ?"))
        .bind(&user.id).bind(group).bind(qq).bind(qq).bind(before).bind(limit).fetch_all(pool).await?)
}
#[derive(Debug, Clone, Serialize, sqlx::FromRow)]
pub struct Fact {
    pub id: String,
    pub user_id: String,
    pub group_id: String,
    pub qq_id: String,
    pub name: String,
    pub content: String,
    pub source: String,
    pub confidence: f64,
    pub evidence: sqlx::types::Json<Value>,
    pub expires_at: i64,
    pub created_at: i64,
    pub updated_at: i64,
    pub deleted_at: Option<i64>,
    pub revision: i64,
    pub indexed_revision: i64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FactInput {
    pub group_id: String,
    pub qq_id: String,
    pub name: String,
    pub content: String,
    pub expires_at: Option<i64>,
}
pub async fn save_fact(
    app: &App,
    user: &User,
    input: &FactInput,
    source: &str,
    confidence: f64,
    evidence: &Value,
) -> Result<String> {
    let cfg = super::config(app, user).await?;
    if !super::valid_id(&cfg.owner_qq) {
        return Err(invalid("请先绑定主人 QQ 号，再创建人物记忆"));
    }
    if !super::valid_id(&input.qq_id)
        || !super::valid_id(&input.group_id)
        || input.name.chars().count() > 80
        || !(3..=500).contains(&input.content.trim().chars().count())
        || !confidence.is_finite()
        || !(0.0..=1.0).contains(&confidence)
    {
        return Err(invalid("人物记忆字段无效"));
    }
    if input.qq_id == cfg.owner_qq {
        crate::memory::remember(app,user,&crate::memory::Input { content:input.content.clone(),importance:3,expires_at:input.expires_at },source,&json!({"channel":"qq_owner","group_id":input.group_id,"qq_id":input.qq_id,"evidence":evidence,"confidence":confidence})).await?;
        let key = digest(
            &input
                .content
                .split_whitespace()
                .collect::<String>()
                .to_lowercase(),
        );
        return Ok(
            sqlx::query_scalar("SELECT id FROM memories WHERE user_id=? AND fact_key=?")
                .bind(&user.id)
                .bind(key)
                .fetch_optional(&app.pool)
                .await?
                .unwrap_or_default(),
        );
    }
    let key = digest(
        &input
            .content
            .split_whitespace()
            .collect::<String>()
            .to_lowercase(),
    );
    let expires = input.expires_at.unwrap_or(now() + 180 * 86400);
    if expires <= now() || expires > now() + 366 * 86400 {
        return Err(invalid("群成员记忆有效期须在未来一年内"));
    }
    let pool = pool(app).await?;
    sqlx::query("INSERT INTO facts(id,user_id,group_id,qq_id,name,fact_key,content,source,confidence,evidence,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,group_id,qq_id,fact_key) DO UPDATE SET content=excluded.content,name=excluded.name,source=excluded.source,confidence=excluded.confidence,evidence=excluded.evidence,expires_at=excluded.expires_at,updated_at=excluded.updated_at,revision=facts.revision+1,deleted_at=NULL WHERE excluded.source='manual' OR (facts.source='auto' AND facts.deleted_at IS NULL)")
        .bind(id()).bind(&user.id).bind(&input.group_id).bind(&input.qq_id).bind(&input.name).bind(&key).bind(input.content.trim()).bind(source).bind(confidence).bind(evidence.to_string()).bind(expires).bind(now()).bind(now()).execute(pool).await?;
    let fact: Fact = sqlx::query_as(
        "SELECT * FROM facts WHERE user_id=? AND group_id=? AND qq_id=? AND fact_key=?",
    )
    .bind(&user.id)
    .bind(&input.group_id)
    .bind(&input.qq_id)
    .bind(key)
    .fetch_one(pool)
    .await?;
    enqueue_index(app, user, &fact).await?;
    Ok(fact.id)
}
pub async fn enqueue_index(app: &App, user: &User, fact: &Fact) -> Result<()> {
    crate::jobs::enqueue(
        app,
        user,
        crate::jobs::Input {
            action: "qq_index".into(),
            params: json!({"id":fact.id}),
            run_at: None,
            request_id: Some(format!("qq-index:{}:{}", fact.id, fact.revision)),
        },
    )
    .await?;
    Ok(())
}
pub async fn fact(app: &App, user: &User, fid: &str) -> Result<Fact> {
    sqlx::query_as("SELECT * FROM facts WHERE id=? AND user_id=?")
        .bind(fid)
        .bind(&user.id)
        .fetch_optional(pool(app).await?)
        .await?
        .ok_or(Error::NotFound)
}
pub async fn facts(app: &App, user: &User, group: &str, qq: &str) -> Result<Vec<Fact>> {
    Ok(sqlx::query_as("SELECT * FROM facts WHERE user_id=? AND group_id=? AND qq_id=? AND deleted_at IS NULL AND expires_at>? ORDER BY updated_at DESC LIMIT 100").bind(&user.id).bind(group).bind(qq).bind(now()).fetch_all(pool(app).await?).await?)
}
pub async fn forget(app: &App, user: &User, fid: &str) -> Result<()> {
    let affected = sqlx::query("UPDATE facts SET deleted_at=?,revision=revision+1,updated_at=? WHERE id=? AND user_id=? AND deleted_at IS NULL").bind(now()).bind(now()).bind(fid).bind(&user.id).execute(pool(app).await?).await?.rows_affected();
    if affected == 0 {
        return Err(Error::NotFound);
    }
    let fact = fact(app, user, fid).await?;
    sqlx::query("DELETE FROM memory_recalls WHERE user_id=? AND scope_id=?")
        .bind(&user.id)
        .bind(format!("{}:{}", fact.group_id, fact.qq_id))
        .execute(pool(app).await?)
        .await?;
    enqueue_index(app, user, &fact).await
}
