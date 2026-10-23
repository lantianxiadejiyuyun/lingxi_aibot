//! Account-owned video reading, with durable runs and a bounded daily budget.
pub mod client;
pub mod reader;
use crate::{
    auth::{self, User},
    db::{id, now},
    error::{Error, Result, invalid},
    settings,
    state::App,
};
use axum::{
    Json,
    extract::{Path, Query, State},
};
use client::{Bili, Credential, Source, VideoId};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

#[derive(Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    pub enabled: bool,
    pub keywords: Vec<String>,
    pub daily_at: String,
    pub daily_limit: i64,
    pub per_run: i64,
    pub recent_days: i64,
    pub profile_id: Option<String>,
    pub taste: String,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            enabled: false,
            keywords: vec![],
            daily_at: "19:00".into(),
            daily_limit: 6,
            per_run: 3,
            recent_days: 7,
            profile_id: None,
            taste: String::new(),
        }
    }
}
impl Config {
    pub fn validate(&mut self) -> Result<()> {
        self.keywords = self
            .keywords
            .iter()
            .map(|s| s.trim().to_owned())
            .filter(|s| !s.is_empty())
            .collect();
        self.keywords.sort();
        self.keywords.dedup();
        if self.keywords.len() > 8
            || self.keywords.iter().any(|s| s.chars().count() > 40)
            || (self.enabled && self.keywords.is_empty())
        {
            return Err(invalid("请填写1至8个兴趣关键词，每个不超过40字"));
        }
        if self.daily_at.len() != 5
            || chrono::NaiveTime::parse_from_str(&self.daily_at, "%H:%M").is_err()
        {
            return Err(invalid("阅读时间须为 HH:MM"));
        }
        if !(1..=20).contains(&self.daily_limit)
            || !(1..=5).contains(&self.per_run)
            || !(1..=30).contains(&self.recent_days)
            || self.taste.chars().count() > 1000
        {
            return Err(invalid(
                "每日1至20条，每次1至5条，发现最近1至30天视频，阅读偏好最多1000字",
            ));
        }
        Ok(())
    }
}
fn ok(v: impl Serialize) -> Json<Value> {
    Json(json!({"ok":true,"data":v}))
}
pub async fn ensure(app: &App, user: &User) -> Result<()> {
    sqlx::query(
        "INSERT INTO bili_state(user_id,config,next_run_at) VALUES(?,?,?) ON CONFLICT DO NOTHING",
    )
    .bind(&user.id)
    .bind(json!(Config::default()).to_string())
    .bind(now() + 86400)
    .execute(&app.pool)
    .await?;
    Ok(())
}
pub async fn config(app: &App, user: &User) -> Result<Config> {
    ensure(app, user).await?;
    let raw: String = sqlx::query_scalar("SELECT config FROM bili_state WHERE user_id=?")
        .bind(&user.id)
        .fetch_one(&app.pool)
        .await?;
    serde_json::from_str(&raw).map_err(|_| invalid("B站设置损坏"))
}
pub async fn credential(app: &App, user: &User) -> Result<Credential> {
    ensure(app, user).await?;
    let raw: String = sqlx::query_scalar("SELECT credential FROM bili_state WHERE user_id=?")
        .bind(&user.id)
        .fetch_one(&app.pool)
        .await?;
    if raw.is_empty() {
        return Ok(Credential::default());
    }
    serde_json::from_str(&app.secrets.decrypt(&raw)?)
        .map_err(|_| invalid("B站凭据损坏，请重新登录"))
}
fn day(user: &User) -> Result<String> {
    let tz: chrono_tz::Tz = user.timezone.parse().map_err(|_| invalid("时区无效"))?;
    Ok(chrono::Utc::now()
        .with_timezone(&tz)
        .format("%Y-%m-%d")
        .to_string())
}
fn next(cfg: &Config, user: &User) -> Result<i64> {
    let (h, m) = cfg
        .daily_at
        .split_once(':')
        .ok_or_else(|| invalid("时间无效"))?;
    crate::jobs::next_run(&format!("{m} {h} * * *"), &user.timezone, now())
}
pub async fn save_api(
    State(app): State<App>,
    user: User,
    Json(mut cfg): Json<Config>,
) -> Result<Json<Value>> {
    cfg.validate()?;
    if cfg.enabled {
        settings::active_profile(&app, &user, cfg.profile_id.as_deref()).await?;
    }
    ensure(&app, &user).await?;
    sqlx::query("UPDATE bili_state SET config=?,revision=revision+1,next_run_at=? WHERE user_id=?")
        .bind(json!(cfg).to_string())
        .bind(next(&cfg, &user)?)
        .bind(&user.id)
        .execute(&app.pool)
        .await?;
    Ok(ok(json!({"saved":true})))
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Input {
    #[serde(default)]
    pub video: String,
    #[serde(default)]
    pub retry: bool,
    pub request_id: String,
}
pub async fn queue(app: &App, user: &User, input: Input, automatic: bool) -> Result<Value> {
    crate::permissions::require(
        app,
        user,
        crate::permissions::Feature::Bilibili,
        crate::permissions::Scope::Private,
    )
    .await?;
    if input.request_id.is_empty() || input.request_id.len() > 100 {
        return Err(invalid("请求标识无效"));
    }
    let cfg = config(app, user).await?;
    if !input.video.is_empty() {
        client::parse_video(&input.video)?;
    } else if cfg.keywords.is_empty() {
        return Err(invalid("先设置兴趣关键词，或粘贴一个视频链接"));
    }
    if automatic && !cfg.enabled {
        return Err(invalid("定时阅读已关闭"));
    }
    settings::active_profile(app, user, cfg.profile_id.as_deref()).await?;
    let encoded = json!(input).to_string();
    let mut tx = app.pool.begin().await?;
    // A write before checking serializes queue admission across server processes.
    sqlx::query("UPDATE bili_state SET next_run_at=next_run_at WHERE user_id=?")
        .bind(&user.id)
        .execute(&mut *tx)
        .await?;
    if let Some((rid, jid, old)) = sqlx::query_as::<_, (String, String, String)>(
        "SELECT id,job_id,input FROM bili_runs WHERE user_id=? AND request_key=?",
    )
    .bind(&user.id)
    .bind(&input.request_id)
    .fetch_optional(&mut *tx)
    .await?
    {
        if old != encoded {
            return Err(Error::Conflict("请求标识已用于其他视频".into()));
        }
        return Ok(json!({"id":rid,"job_id":jid}));
    }
    let active: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM bili_runs WHERE user_id=? AND status IN ('queued','running')",
    )
    .bind(&user.id)
    .fetch_one(&mut *tx)
    .await?;
    if active > 0 {
        return Err(Error::Conflict("已有阅读任务，请等待完成或先取消".into()));
    }
    let recent: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM bili_runs WHERE user_id=? AND created_at>?")
            .bind(&user.id)
            .bind(now() - 86400)
            .fetch_one(&mut *tx)
            .await?;
    if recent >= 40 {
        return Err(invalid("今天已提交较多任务，请明天再试"));
    }
    let used: i64 = sqlx::query_scalar(
        "SELECT COALESCE((SELECT attempts FROM bili_usage WHERE user_id=? AND day=?),0)",
    )
    .bind(&user.id)
    .bind(day(user)?)
    .fetch_one(&mut *tx)
    .await?;
    if used >= cfg.daily_limit {
        return Err(invalid("今天的阅读次数已用完，可明天继续或调整每日上限"));
    }
    let revision: i64 = sqlx::query_scalar("SELECT revision FROM bili_state WHERE user_id=?")
        .bind(&user.id)
        .fetch_one(&mut *tx)
        .await?;
    let rid = id();
    let jid = id();
    sqlx::query("INSERT INTO jobs(id,user_id,action,params,max_attempts,run_at,created_at,updated_at) VALUES(?,?,'bili_read',?,1,?,?,?)").bind(&jid).bind(&user.id).bind(json!({"run_id":rid}).to_string()).bind(now()).bind(now()).bind(now()).execute(&mut *tx).await?;
    sqlx::query("INSERT INTO bili_runs(id,user_id,job_id,request_key,mode,input,config,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)").bind(&rid).bind(&user.id).bind(&jid).bind(&input.request_id).bind(if automatic{"automatic"}else{"manual"}).bind(encoded).bind(json!(cfg).to_string()).bind(revision).bind(now()).bind(now()).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(json!({"id":rid,"job_id":jid}))
}
pub async fn run_api(
    State(app): State<App>,
    user: User,
    Json(input): Json<Input>,
) -> Result<Json<Value>> {
    recover(&app).await?;
    Ok(ok(queue(&app, &user, input, false).await?))
}
pub async fn recover(app: &App) -> Result<()> {
    sqlx::query("UPDATE bili_runs SET status='interrupted',progress='任务中断或已取消，可手动重试',updated_at=? WHERE status IN ('queued','running') AND job_id IN (SELECT id FROM jobs WHERE status IN ('cancelled','failed','needs_review') OR (status='running' AND lease_until<?))").bind(now()).bind(now()).execute(&app.pool).await?;
    sqlx::query("UPDATE bili_entries SET status='failed',reason='阅读被中断，未标记为已读',updated_at=? WHERE status='reading' AND run_id IN (SELECT id FROM bili_runs WHERE status='interrupted')").bind(now()).execute(&app.pool).await?;
    Ok(())
}
pub async fn reconcile(app: &App) -> Result<()> {
    recover(app).await?;
    let due:Vec<(String,String,i64)>=sqlx::query_as("SELECT user_id,config,next_run_at FROM bili_state WHERE json_extract(config,'$.enabled')=1 AND next_run_at<=? LIMIT 50").bind(now()).fetch_all(&app.pool).await?;
    for (uid, raw, at) in due {
        let user = auth::user(app, &uid).await?;
        let cfg: Config = serde_json::from_str(&raw).map_err(|_| invalid("B站配置无效"))?;
        if sqlx::query("UPDATE bili_state SET next_run_at=? WHERE user_id=? AND next_run_at=?")
            .bind(next(&cfg, &user)?)
            .bind(&uid)
            .bind(at)
            .execute(&app.pool)
            .await?
            .rows_affected()
            == 1
        {
            if !crate::permissions::allowed(
                app,
                &user,
                crate::permissions::Feature::Bilibili,
                crate::permissions::Scope::Private,
            )
            .await?
            {
                continue;
            }
            if let Err(e) = queue(
                app,
                &user,
                Input {
                    video: String::new(),
                    retry: false,
                    request_id: format!("schedule:{at}"),
                },
                true,
            )
            .await
            {
                crate::jobs::notify(
                    app,
                    &user,
                    "B 站阅读暂未开始",
                    &e.to_string(),
                    &format!("bili-skip:{uid}:{at}"),
                )
                .await?;
            }
        }
    }
    Ok(())
}
#[derive(Default, Deserialize)]
pub struct Filter {
    pub q: Option<String>,
    pub status: Option<String>,
    pub favorite: Option<bool>,
    pub page: Option<i64>,
}
pub async fn list(
    State(app): State<App>,
    user: User,
    Query(f): Query<Filter>,
) -> Result<Json<Value>> {
    recover(&app).await?;
    let cfg = config(&app, &user).await?;
    let q = f.q.unwrap_or_default();
    if q.chars().count() > 100 {
        return Err(invalid("搜索内容太长"));
    }
    let status = f.status.unwrap_or_default();
    let page = f.page.unwrap_or(1).clamp(1, 10000);
    let clause = "FROM bili_entries WHERE user_id=? AND (?='' OR instr(title,?)>0 OR instr(author,?)>0 OR instr(analysis,?)>0) AND (?='' OR status=?) AND (?=0 OR favorite=1)";
    let rows:Vec<(String,String,i64,String,String,i64,String,String,String,String,sqlx::types::Json<Value>,bool,i64)>=sqlx::query_as(&format!("SELECT id,bvid,page,title,author,duration,status,reason,source,language,analysis,favorite,updated_at {clause} ORDER BY updated_at DESC,id DESC LIMIT 18 OFFSET ?")).bind(&user.id).bind(&q).bind(&q).bind(&q).bind(&q).bind(&status).bind(&status).bind(f.favorite.unwrap_or(false)).bind((page-1)*18).fetch_all(&app.pool).await?;
    let total: i64 = sqlx::query_scalar(&format!("SELECT COUNT(*) {clause}"))
        .bind(&user.id)
        .bind(&q)
        .bind(&q)
        .bind(&q)
        .bind(&q)
        .bind(&status)
        .bind(&status)
        .bind(f.favorite.unwrap_or(false))
        .fetch_one(&app.pool)
        .await?;
    let items:Vec<Value>=rows.into_iter().map(|(id,bvid,page,title,author,duration,status,reason,source,language,analysis,favorite,updated_at)|json!({"id":id,"url":client::video_url(&bvid,page as u32),"bvid":bvid,"page":page,"title":title,"author":author,"duration":duration,"status":status,"reason":reason,"source":source,"language":language,"analysis":analysis.0,"favorite":favorite,"updated_at":updated_at})).collect();
    let stats:(i64,i64,i64)=sqlx::query_as("SELECT COUNT(*),COALESCE(SUM(status='completed'),0),COALESCE(SUM(favorite),0) FROM bili_entries WHERE user_id=?").bind(&user.id).fetch_one(&app.pool).await?;
    let (connected, name, next_at): (bool, String, i64) = sqlx::query_as(
        "SELECT credential!='',account_name,next_run_at FROM bili_state WHERE user_id=?",
    )
    .bind(&user.id)
    .fetch_one(&app.pool)
    .await?;
    let usage: i64 = sqlx::query_scalar(
        "SELECT COALESCE((SELECT attempts FROM bili_usage WHERE user_id=? AND day=?),0)",
    )
    .bind(&user.id)
    .bind(day(&user)?)
    .fetch_one(&app.pool)
    .await?;
    let runs:Vec<(String,String,String,String,String,i64)>=sqlx::query_as("SELECT id,job_id,status,progress,report,created_at FROM bili_runs WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT 10").bind(&user.id).fetch_all(&app.pool).await?;
    Ok(ok(
        json!({"config":cfg,"items":items,"total":total,"page":page,"timezone":user.timezone,"next_run_at":next_at,"account":{"connected":connected,"name":name},"stats":{"total":stats.0,"completed":stats.1,"favorites":stats.2,"today":usage},"runs":runs.into_iter().map(|(id,job_id,status,progress,report,created_at)|json!({"id":id,"job_id":job_id,"status":status,"progress":progress,"report":report,"created_at":created_at})).collect::<Vec<_>>()}),
    ))
}
pub async fn detail(
    State(app): State<App>,
    user: User,
    Path(eid): Path<String>,
) -> Result<Json<Value>> {
    let (segments, analysis): (sqlx::types::Json<Value>, sqlx::types::Json<Value>) =
        sqlx::query_as("SELECT segments,analysis FROM bili_entries WHERE user_id=? AND id=?")
            .bind(&user.id)
            .bind(eid)
            .fetch_optional(&app.pool)
            .await?
            .ok_or(Error::NotFound)?;
    Ok(ok(json!({"segments":segments.0,"analysis":analysis.0})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Mark {
    pub favorite: bool,
}
pub async fn mark(
    State(app): State<App>,
    user: User,
    Path(eid): Path<String>,
    Json(m): Json<Mark>,
) -> Result<Json<Value>> {
    if sqlx::query("UPDATE bili_entries SET favorite=? WHERE user_id=? AND id=?")
        .bind(m.favorite)
        .bind(&user.id)
        .bind(eid)
        .execute(&app.pool)
        .await?
        .rows_affected()
        == 0
    {
        return Err(Error::NotFound);
    }
    Ok(ok(json!({"saved":true})))
}
pub async fn delete(
    State(app): State<App>,
    user: User,
    Path(eid): Path<String>,
) -> Result<Json<Value>> {
    let mut tx = app.pool.begin().await?;
    let status: Option<String> =
        sqlx::query_scalar("SELECT status FROM bili_entries WHERE user_id=? AND id=?")
            .bind(&user.id)
            .bind(&eid)
            .fetch_optional(&mut *tx)
            .await?;
    match status.as_deref() {
        None => return Err(Error::NotFound),
        Some("reading") => return Err(Error::Conflict("请先取消正在进行的阅读".into())),
        _ => (),
    };
    sqlx::query("DELETE FROM bili_entries WHERE user_id=? AND id=?")
        .bind(&user.id)
        .bind(eid)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(ok(json!({"deleted":true})))
}

pub async fn login_start(State(app): State<App>, user: User) -> Result<Json<Value>> {
    ensure(&app, &user).await?;
    let mut tx = app.pool.begin().await?;
    let exists: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM bili_login WHERE user_id=? AND expires_at>?")
            .bind(&user.id)
            .bind(now() + 150)
            .fetch_one(&mut *tx)
            .await?;
    if exists > 0 {
        return Err(Error::Busy);
    }
    tx.commit().await?;
    let (key, url) = Bili::new(&app, Credential::default()).qr_start().await?;
    let login_id = id();
    sqlx::query("INSERT INTO bili_login(user_id,id,secret,expires_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET id=excluded.id,secret=excluded.secret,expires_at=excluded.expires_at,polled_at=0").bind(&user.id).bind(&login_id).bind(app.secrets.encrypt(&key)?).bind(now()+180).execute(&app.pool).await?;
    Ok(ok(json!({"id":login_id,"url":url,"expires_at":now()+180})))
}
pub async fn login_poll(
    State(app): State<App>,
    user: User,
    Path(lid): Path<String>,
) -> Result<Json<Value>> {
    let row:Option<(String,i64)>=sqlx::query_as("UPDATE bili_login SET polled_at=? WHERE user_id=? AND id=? AND polled_at<=? RETURNING secret,expires_at").bind(now()).bind(&user.id).bind(&lid).bind(now()-3).fetch_optional(&app.pool).await?;
    let Some((key, expires)) = row else {
        return Err(Error::Busy);
    };
    if expires <= now() {
        return Ok(ok(json!({"state":"expired"})));
    }
    let (state, cred) = Bili::new(&app, Credential::default())
        .qr_poll(&app.secrets.decrypt(&key)?)
        .await?;
    if let Some(cred) = cred {
        let name = Bili::new(&app, cred.clone()).account().await?;
        let encrypted = app.secrets.encrypt(&json!(cred).to_string())?;
        let mut tx = app.pool.begin().await?;
        let claimed = sqlx::query("DELETE FROM bili_login WHERE user_id=? AND id=?")
            .bind(&user.id)
            .bind(&lid)
            .execute(&mut *tx)
            .await?
            .rows_affected();
        if claimed == 0 {
            return Err(Error::Conflict("此二维码已失效".into()));
        }
        sqlx::query("UPDATE bili_state SET credential=?,account_name=? WHERE user_id=?")
            .bind(encrypted)
            .bind(name)
            .bind(&user.id)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
    }
    Ok(ok(json!({"state":state})))
}
pub async fn disconnect(State(app): State<App>, user: User) -> Result<Json<Value>> {
    let mut tx = app.pool.begin().await?;
    sqlx::query("UPDATE bili_state SET credential='',account_name='' WHERE user_id=?")
        .bind(&user.id)
        .execute(&mut *tx)
        .await?;
    sqlx::query("DELETE FROM bili_login WHERE user_id=?")
        .bind(&user.id)
        .execute(&mut *tx)
        .await?;
    sqlx::query("UPDATE jobs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE user_id=? AND action='bili_read' AND status IN ('queued','running')").bind(&user.id).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(ok(json!({"disconnected":true})))
}

async fn progress(app: &App, rid: &str, text: &str) -> Result<()> {
    sqlx::query("UPDATE bili_runs SET progress=?,updated_at=? WHERE id=? AND status='running'")
        .bind(text)
        .bind(now())
        .bind(rid)
        .execute(&app.pool)
        .await?;
    Ok(())
}
pub async fn process(
    app: &App,
    user: &User,
    params: &Value,
    cancel: &CancellationToken,
) -> Result<Value> {
    if let Err(error) = crate::permissions::require(
        app,
        user,
        crate::permissions::Feature::Bilibili,
        crate::permissions::Scope::Private,
    )
    .await
    {
        sqlx::query("UPDATE bili_runs SET status='failed',progress=?,updated_at=? WHERE id=? AND user_id=? AND status='queued'").bind(error.to_string()).bind(now()).bind(params["run_id"].as_str().unwrap_or("")).bind(&user.id).execute(&app.pool).await?;
        return Err(error);
    }
    let mut source = Bili::new(app, credential(app, user).await?);
    if let Err(error) = source.prepare().await {
        let message = match &error {
            Error::Provider(s) | Error::Invalid(s) => s.as_str(),
            _ => "B 站连接准备失败",
        };
        sqlx::query("UPDATE bili_runs SET status='failed',progress=?,updated_at=? WHERE id=? AND user_id=? AND status='queued'")
            .bind(message).bind(now()).bind(params["run_id"].as_str().unwrap_or("")).bind(&user.id).execute(&app.pool).await?;
        return Err(error);
    }
    process_with(
        app,
        user,
        params["run_id"].as_str().unwrap_or(""),
        cancel,
        &source,
    )
    .await
}
pub async fn process_with(
    app: &App,
    user: &User,
    rid: &str,
    cancel: &CancellationToken,
    source: &impl Source,
) -> Result<Value> {
    let row:Option<(String,String,String,i64)>=sqlx::query_as("UPDATE bili_runs SET status='running',progress='准备阅读',updated_at=? WHERE user_id=? AND id=? AND status='queued' RETURNING input,config,mode,revision").bind(now()).bind(&user.id).bind(rid).fetch_optional(&app.pool).await?;
    let Some((input, cfg, mode, revision)) = row else {
        return Err(Error::Conflict("阅读任务不在等待状态".into()));
    };
    let result = run_inner(
        app, user, rid, &input, &cfg, &mode, revision, cancel, source,
    )
    .await;
    match result {
        Ok((status, report)) => {
            sqlx::query("UPDATE bili_runs SET status=?,progress='本轮阅读结束',report=?,updated_at=? WHERE id=? AND user_id=? AND status='running'").bind(&status).bind(&report).bind(now()).bind(rid).bind(&user.id).execute(&app.pool).await?;
            Ok(json!({"status":status,"run_id":rid,"report":report}))
        }
        Err(e) => {
            let message = match &e {
                Error::Invalid(s) | Error::Provider(s) | Error::Conflict(s) => s.clone(),
                Error::Cancelled => "阅读已取消".into(),
                _ => "阅读未完成，请稍后重试".into(),
            };
            sqlx::query("UPDATE bili_runs SET status='failed',progress=?,updated_at=? WHERE id=? AND user_id=? AND status='running'").bind(&message).bind(now()).bind(rid).bind(&user.id).execute(&app.pool).await?;
            sqlx::query("UPDATE bili_entries SET status='failed',reason=?,updated_at=? WHERE run_id=? AND status='reading'").bind(&message).bind(now()).bind(rid).execute(&app.pool).await?;
            Err(e)
        }
    }
}
#[allow(clippy::too_many_arguments)]
async fn run_inner(
    app: &App,
    user: &User,
    rid: &str,
    input: &str,
    cfg: &str,
    mode: &str,
    revision: i64,
    cancel: &CancellationToken,
    source: &impl Source,
) -> Result<(String, String)> {
    crate::permissions::require(
        app,
        user,
        crate::permissions::Feature::Bilibili,
        crate::permissions::Scope::Private,
    )
    .await?;
    let input: Input = serde_json::from_str(input).map_err(|_| invalid("阅读输入损坏"))?;
    let cfg: Config = serde_json::from_str(cfg).map_err(|_| invalid("阅读设置损坏"))?;
    if mode == "automatic" {
        let current: i64 = sqlx::query_scalar("SELECT revision FROM bili_state WHERE user_id=?")
            .bind(&user.id)
            .fetch_one(&app.pool)
            .await?;
        if revision != current || !config(app, user).await?.enabled {
            return Err(invalid("设置已变更，本轮定时阅读取消"));
        }
    }
    let profile = settings::active_profile(app, user, cfg.profile_id.as_deref()).await?;
    let mut candidates = Vec::new();
    let mut warnings = Vec::new();
    if !input.video.is_empty() {
        candidates.push(client::parse_video(&input.video)?);
    } else {
        for kw in &cfg.keywords {
            if cancel.is_cancelled() {
                return Err(Error::Cancelled);
            }
            crate::permissions::require(
                app,
                user,
                crate::permissions::Feature::Bilibili,
                crate::permissions::Scope::Private,
            )
            .await?;
            progress(app, rid, &format!("正在发现：{kw}")).await?;
            match source.search(kw, cfg.recent_days).await {
                Ok(v) => {
                    for id in v {
                        if !candidates.contains(&id) {
                            candidates.push(id)
                        }
                    }
                }
                Err(e) => {
                    warnings.push(format!("关键词「{kw}」：{e}"));
                    break;
                }
            }
        }
    }
    let mut report = format!("# B 站阅读小记 · {}\n\n", day(user)?);
    let mut attempted = 0;
    let mut completed = 0;
    let mut failed = 0;
    for video_id in candidates {
        crate::permissions::require(
            app,
            user,
            crate::permissions::Feature::Bilibili,
            crate::permissions::Scope::Private,
        )
        .await?;
        if cancel.is_cancelled() {
            return Err(Error::Cancelled);
        }
        if attempted >= cfg.per_run {
            break;
        }
        let previous: Option<(String, String)> = sqlx::query_as(
            "SELECT id,status FROM bili_entries WHERE user_id=? AND bvid=? AND page=?",
        )
        .bind(&user.id)
        .bind(&video_id.bvid)
        .bind(video_id.page)
        .fetch_optional(&app.pool)
        .await?;
        if previous.is_some() && !input.retry {
            continue;
        }
        let mut tx = app.pool.begin().await?;
        let consumed=sqlx::query("INSERT INTO bili_usage(user_id,day,attempts) VALUES(?,?,1) ON CONFLICT(user_id,day) DO UPDATE SET attempts=attempts+1 WHERE attempts<?").bind(&user.id).bind(day(user)?).bind(cfg.daily_limit).execute(&mut *tx).await?.rows_affected();
        if consumed == 0 {
            warnings.push("今日次数已用完，余下视频留待下次。".into());
            break;
        }
        let eid = previous.map(|p| p.0).unwrap_or_else(id);
        sqlx::query("INSERT INTO bili_entries(id,user_id,run_id,bvid,page,title,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,bvid,page) DO UPDATE SET run_id=excluded.run_id,status='reading',reason='',analysis='{}',segments='[]',source='',language='',updated_at=excluded.updated_at").bind(&eid).bind(&user.id).bind(rid).bind(&video_id.bvid).bind(video_id.page).bind(&video_id.bvid).bind(now()).bind(now()).execute(&mut *tx).await?;
        tx.commit().await?;
        attempted += 1;
        progress(
            app,
            rid,
            &format!("正在阅读第 {attempted} 条 · {}", video_id.bvid),
        )
        .await?;
        let result = read_one(app, user, &eid, &video_id, &profile, &cfg, cancel, source).await;
        let url = client::video_url(&video_id.bvid, video_id.page);
        match result {
            Ok(Some((title, a))) => {
                completed += 1;
                report.push_str(&format!(
                    "## {title}\n\n{url}\n\n{}\n\n{}\n\n",
                    a.summary, a.thought
                ));
            }
            Ok(None) => {
                report.push_str(&format!(
                    "- {}：当前没有取得字幕，已跳过。\n",
                    video_id.bvid
                ));
            }
            Err(Error::Cancelled) => return Err(Error::Cancelled),
            Err(e) => {
                failed += 1;
                let reason = match e {
                    Error::Provider(s) | Error::Invalid(s) => s,
                    _ => "阅读失败，请稍后重试".into(),
                };
                sqlx::query("UPDATE bili_entries SET status='failed',reason=?,updated_at=? WHERE id=? AND user_id=?").bind(&reason).bind(now()).bind(&eid).bind(&user.id).execute(&app.pool).await?;
                report.push_str(&format!("- {}：{reason}\n", video_id.bvid));
                if reason.contains("B 站") {
                    warnings.push("本轮停止访问 B 站，避免连续重试。".into());
                    break;
                }
            }
        }
    }
    if attempted == 0 {
        report
            .push_str("本轮没有新的可读视频：可能已在阅读记录中，或没有找到符合时间范围的视频。\n");
    }
    for warning in &warnings {
        report.push_str(&format!("\n{warning}\n"));
    }
    report.push_str(&format!(
        "\n本轮尝试 {attempted} 条，读完字幕 {completed} 条。未读取画面或音频。"
    ));
    Ok((
        if completed > 0 && (failed > 0 || !warnings.is_empty()) {
            "partial"
        } else if failed > 0 || !warnings.is_empty() {
            "failed"
        } else {
            "completed"
        }
        .into(),
        report,
    ))
}
#[allow(clippy::too_many_arguments)]
async fn read_one(
    app: &App,
    user: &User,
    eid: &str,
    video_id: &VideoId,
    profile: &settings::Profile,
    cfg: &Config,
    cancel: &CancellationToken,
    source: &impl Source,
) -> Result<Option<(String, reader::Analysis)>> {
    let video = source.info(video_id).await?;
    sqlx::query("UPDATE bili_entries SET title=?,author=?,duration=? WHERE id=? AND user_id=?")
        .bind(&video.title)
        .bind(&video.author)
        .bind(video.duration)
        .bind(eid)
        .bind(&user.id)
        .execute(&app.pool)
        .await?;
    let Some(captions) = source.captions(video_id, &video).await? else {
        sqlx::query("UPDATE bili_entries SET status='skipped',reason='当前账号没有取得字幕；可扫码后重试。未读取音频或画面。',updated_at=? WHERE id=? AND user_id=?").bind(now()).bind(eid).bind(&user.id).execute(&app.pool).await?;
        return Ok(None);
    };
    let analysis = reader::read(
        app,
        user,
        profile,
        &video.title,
        &captions.segments,
        &cfg.taste,
        cancel,
    )
    .await?;
    if cancel.is_cancelled() {
        return Err(Error::Cancelled);
    }
    sqlx::query("UPDATE bili_entries SET status='completed',reason='',source='bilibili_subtitles',language=?,segments=?,analysis=?,updated_at=? WHERE id=? AND user_id=? AND status='reading'").bind(&captions.language).bind(json!(captions.segments).to_string()).bind(json!(analysis).to_string()).bind(now()).bind(eid).bind(&user.id).execute(&app.pool).await?;
    Ok(Some((video.title, analysis)))
}
pub async fn companion_sources(app: &App, user: &User, at: i64) -> Result<Vec<Value>> {
    if !crate::permissions::allowed(
        app,
        user,
        crate::permissions::Feature::Bilibili,
        crate::permissions::Scope::Private,
    )
    .await?
    {
        return Ok(vec![]);
    }
    let rows:Vec<(String,String,i64,String,sqlx::types::Json<Value>,i64)>=sqlx::query_as("SELECT id,bvid,page,title,analysis,updated_at FROM bili_entries WHERE user_id=? AND status='completed' AND updated_at>=? AND json_extract(analysis,'$.worth_reading')=1 ORDER BY updated_at DESC LIMIT 3").bind(&user.id).bind(at-7*86400).fetch_all(&app.pool).await?;
    Ok(rows.into_iter().map(|(id,bvid,page,title,a,t)|json!({"id":format!("bili-read:{id}"),"kind":"bilibili_subtitles","title":title,"snippet":a.0["summary"],"thought":a.0["thought"],"url":client::video_url(&bvid,page as u32),"observed_at":t})).collect())
}
