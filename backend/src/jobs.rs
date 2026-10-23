use crate::{
    auth::{self, User},
    db::{id, now},
    error::{Error, Result, invalid},
    state::App,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{str::FromStr, time::Duration};
use tokio_util::sync::CancellationToken;

pub const ACTIONS: &[&str] = &[
    "persona_reflect",
    "bili_read",
    "companion_tick",
    "qq_private",
    "qq_outreach",
    "qq_sticker",
    "qq_event",
    "qq_extract",
    "qq_index",
    "skill",
    "index",
    "notify",
    "reminders",
    "consolidate",
    "briefing",
    "research",
    "image",
    "download",
    "webhook",
];
#[derive(Deserialize, Serialize, Clone)]
#[serde(deny_unknown_fields)]
pub struct Input {
    pub action: String,
    #[serde(default = "object")]
    pub params: Value,
    pub run_at: Option<i64>,
    pub request_id: Option<String>,
}
fn object() -> Value {
    json!({})
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ScheduleInput {
    pub name: String,
    pub action: String,
    #[serde(default = "object")]
    pub params: Value,
    pub cron: String,
    pub timezone: String,
    #[serde(default = "enabled")]
    pub enabled: bool,
}
fn enabled() -> bool {
    true
}
#[derive(Serialize, sqlx::FromRow, Clone)]
pub struct Job {
    pub id: String,
    pub user_id: String,
    pub action: String,
    pub params: sqlx::types::Json<Value>,
    pub status: String,
    pub attempts: i64,
    pub max_attempts: i64,
    pub run_at: i64,
    pub result: Option<String>,
    pub error: String,
    pub created_at: i64,
    pub updated_at: i64,
}
pub fn next_run(cron: &str, tz: &str, after: i64) -> Result<i64> {
    let cron = if cron.split_whitespace().count() == 5 {
        format!("0 {cron}")
    } else {
        cron.into()
    };
    let schedule = cron::Schedule::from_str(&cron).map_err(|_| invalid("cron 表达式无效"))?;
    let timezone = chrono_tz::Tz::from_str(tz).map_err(|_| invalid("时区无效"))?;
    let after = chrono::DateTime::from_timestamp(after, 0)
        .ok_or_else(|| invalid("时间无效"))?
        .with_timezone(&timezone);
    schedule
        .after(&after)
        .next()
        .map(|t| t.timestamp())
        .ok_or_else(|| invalid("计划没有下一次运行时间"))
}
fn validate(action: &str, params: &Value) -> Result<()> {
    if !ACTIONS.contains(&action) || !params.is_object() || params.to_string().len() > 64_000 {
        return Err(invalid("任务动作或参数无效"));
    }
    Ok(())
}
pub async fn enqueue(app: &App, user: &User, input: Input) -> Result<Job> {
    validate(&input.action, &input.params)?;
    if input
        .request_id
        .as_ref()
        .is_some_and(|id| id.is_empty() || id.len() > 200)
    {
        return Err(invalid("request_id 无效"));
    }
    let jid = id();
    let retry = if [
        "notify",
        "reminders",
        "consolidate",
        "briefing",
        "qq_index",
        "qq_extract",
        "qq_sticker",
    ]
    .contains(&input.action.as_str())
    {
        3
    } else {
        1
    };
    sqlx::query("INSERT INTO jobs(id,user_id,action,params,max_attempts,run_at,dedupe_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,dedupe_key) DO NOTHING")
        .bind(&jid).bind(&user.id).bind(&input.action).bind(input.params.to_string()).bind(retry)
        .bind(input.run_at.unwrap_or_else(now)).bind(&input.request_id).bind(now()).bind(now()).execute(&app.pool).await?;
    let job = if let Some(key) = input.request_id {
        sqlx::query_as::<_, Job>("SELECT * FROM jobs WHERE user_id=? AND dedupe_key=?")
            .bind(&user.id)
            .bind(key)
            .fetch_one(&app.pool)
            .await?
    } else {
        get(app, user, &jid).await?
    };
    if job.action != input.action || job.params.0 != input.params {
        return Err(Error::Conflict("request_id 已用于其他任务".into()));
    }
    Ok(job)
}
pub async fn get(app: &App, user: &User, jid: &str) -> Result<Job> {
    sqlx::query_as("SELECT * FROM jobs WHERE id=? AND user_id=?")
        .bind(jid)
        .bind(&user.id)
        .fetch_optional(&app.pool)
        .await?
        .ok_or(Error::NotFound)
}
pub async fn add_schedule(app: &App, user: &User, input: ScheduleInput) -> Result<String> {
    validate(&input.action, &input.params)?;
    if input.name.trim().is_empty() || input.name.chars().count() > 128 {
        return Err(invalid("任务名称无效"));
    }
    let next = next_run(&input.cron, &input.timezone, now())?;
    let sid = id();
    sqlx::query("INSERT INTO schedules(id,user_id,name,action,params,cron,timezone,enabled,next_run_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)")
        .bind(&sid).bind(&user.id).bind(input.name).bind(input.action).bind(input.params.to_string())
        .bind(input.cron).bind(input.timezone).bind(input.enabled).bind(next).bind(now()).execute(&app.pool).await?;
    Ok(sid)
}
pub async fn notify(app: &App, user: &User, title: &str, body: &str, key: &str) -> Result<()> {
    sqlx::query("INSERT INTO notifications(id,user_id,title,body,dedupe_key,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,dedupe_key) DO NOTHING")
        .bind(id()).bind(&user.id).bind(title).bind(body).bind(key).bind(now()).execute(&app.pool).await?;
    Ok(())
}
pub async fn tick_schedules(app: &App) -> Result<()> {
    type Due = (String, String, String, String, String, String, i64);
    let rows:Vec<Due>=sqlx::query_as("SELECT id,user_id,action,params,cron,timezone,next_run_at FROM schedules WHERE enabled=1 AND next_run_at<=? LIMIT 100")
        .bind(now()).fetch_all(&app.pool).await?;
    for (sid, uid, action, params, cron, tz, due) in rows {
        let next = next_run(&cron, &tz, now())?;
        let mut tx = app.pool.begin().await?;
        let changed = sqlx::query(
            "UPDATE schedules SET next_run_at=? WHERE id=? AND next_run_at=? AND enabled=1",
        )
        .bind(next)
        .bind(&sid)
        .bind(due)
        .execute(&mut *tx)
        .await?
        .rows_affected();
        if changed == 1 {
            sqlx::query("INSERT INTO jobs(id,user_id,action,params,run_at,dedupe_key,max_attempts,created_at,updated_at) VALUES(?,?,?,?,?,?,1,?,?) ON CONFLICT(user_id,dedupe_key) DO NOTHING")
                .bind(id()).bind(uid).bind(action).bind(params).bind(now()).bind(format!("schedule:{sid}:{due}")).bind(now()).bind(now()).execute(&mut *tx).await?;
        }
        tx.commit().await?;
    }
    Ok(())
}
pub async fn claim(app: &App, token: &str) -> Result<Option<Job>> {
    // Side-effectful jobs with exhausted attempts are not blindly repeated after a crash.
    sqlx::query("UPDATE jobs SET status='needs_review',error='进程中断，外部操作结果待核实',lease_token=NULL,lease_until=NULL,updated_at=? WHERE status='running' AND lease_until<? AND attempts>=max_attempts")
        .bind(now()).bind(now()).execute(&app.pool).await?;
    Ok(sqlx::query_as("UPDATE jobs SET status='running',attempts=attempts+1,lease_token=?,lease_until=?,updated_at=? WHERE id=(SELECT id FROM jobs WHERE cancel_requested=0 AND attempts<max_attempts AND ((status='queued' AND run_at<=?) OR (status='running' AND lease_until<?)) ORDER BY run_at,created_at LIMIT 1) RETURNING *")
        .bind(token).bind(now()+90).bind(now()).bind(now()).bind(now()).fetch_optional(&app.pool).await?)
}
pub async fn worker(app: App) {
    let mut running = tokio::task::JoinSet::new();
    let mut interval = tokio::time::interval(Duration::from_secs(1));
    let mut qq_interval = tokio::time::interval(Duration::from_secs(60));
    loop {
        tokio::select! {
            _=app.shutdown.cancelled()=>break,
            _=qq_interval.tick()=>{
                if let Err(error)=crate::qq::worker::reconcile(&app).await{tracing::error!(%error,"QQ outbox recovery failed");}
                if let Err(error)=crate::qq::outreach::reconcile(&app).await{tracing::error!(%error,"QQ outreach recovery failed");}
                if let Err(error)=crate::growth::reconcile(&app).await{tracing::error!(%error,"persona growth dispatch failed");}
                if let Err(error)=crate::companion::reconcile(&app).await{tracing::error!(%error,"companion recovery failed");}
                if let Err(error)=crate::bilibili::reconcile(&app).await{tracing::error!(%error,"Bilibili recovery failed");}
            },
            Some(result)=running.join_next(),if !running.is_empty()=>{if let Err(error)=result{tracing::error!(%error,"job task panicked");}},
            _=interval.tick()=>{
                if let Err(error)=crate::feishu::reconcile(&app).await{tracing::error!(%error,"Feishu inbox recovery failed");}
                if let Err(error)=tick_schedules(&app).await{tracing::error!(%error,"schedule dispatch failed");}
                while running.len()<4{
                    let token=id();match claim(&app,&token).await {
                        Ok(Some(job))=>{let app=app.clone();running.spawn(async move{run_job(app,job,token).await;});},
                        Ok(None)=>break,Err(error)=>{tracing::error!(%error,"job claim failed");break;}
                    }
                }
            }
        }
    }
    while running.join_next().await.is_some() {}
}
async fn run_job(app: App, job: Job, token: String) {
    let cancel = CancellationToken::new();
    let stop = CancellationToken::new();
    let heartbeat_app = app.clone();
    let heartbeat_job = job.id.clone();
    let heartbeat_token = token.clone();
    let heartbeat_cancel = cancel.clone();
    let heartbeat_stop = stop.clone();
    let heartbeat = tokio::spawn(async move {
        loop {
            tokio::select! {_=heartbeat_stop.cancelled()=>break,_=tokio::time::sleep(Duration::from_secs(5))=>()}
            let result=sqlx::query("UPDATE jobs SET lease_until=? WHERE id=? AND lease_token=? AND status='running' AND cancel_requested=0 AND lease_until>?")
            .bind(now()+90).bind(&heartbeat_job).bind(&heartbeat_token).bind(now()).execute(&heartbeat_app.pool).await;
            if !result.is_ok_and(|r| r.rows_affected() == 1) {
                heartbeat_cancel.cancel();
                break;
            }
        }
    });
    let result = tokio::select! {
        _=app.shutdown.cancelled()=>Err(Error::Cancelled),_=cancel.cancelled()=>Err(Error::Cancelled),
        _=tokio::time::sleep(Duration::from_secs(600))=>Err(Error::Provider("后台任务超过 10 分钟时限".into())),
        result=execute(&app,&job,&cancel)=>result
    };
    stop.cancel();
    let _ = heartbeat.await;
    let (status, value, error) = match result {
        Ok(value) => ("completed", Some(value.to_string()), String::new()),
        Err(Error::Cancelled) => (
            "cancelled",
            None,
            "任务已中断；已完成的外部操作不会撤销".into(),
        ),
        Err(error) => {
            tracing::warn!(job_id=%job.id,%error,"job failed");
            (
                if job.attempts < job.max_attempts {
                    "queued"
                } else {
                    "failed"
                },
                None,
                "任务执行失败，详情见服务日志".into(),
            )
        }
    };
    let _=sqlx::query("UPDATE jobs SET status=?,result=?,error=?,run_at=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=? AND lease_token=?")
        .bind(status).bind(value).bind(error).bind(now()+30*job.attempts.pow(2)).bind(now()).bind(&job.id).bind(token).execute(&app.pool).await;
}
async fn execute(app: &App, job: &Job, cancel: &CancellationToken) -> Result<Value> {
    let user = auth::user(app, &job.user_id).await?;
    let params = &job.params.0;
    match job.action.as_str() {
        "feishu_message" => crate::feishu::process(app, &user, params, cancel).await,
        "persona_reflect" => crate::growth::reflect(app, &user, params, cancel).await,
        "bili_read" => crate::bilibili::process(app, &user, params, cancel).await,
        "companion_tick" => crate::companion::tick(app, &user, cancel, None).await,
        "qq_private" => crate::companion::private::process_qq(app, &user, params, cancel).await,
        "qq_outreach" => crate::qq::outreach::process(app, &user, params, cancel).await,
        "qq_sticker" => crate::qq::stickers::process(app, &user, params, cancel).await,
        "qq_event" => crate::qq::worker::process(app, &user, params, cancel).await,
        "qq_extract" => crate::qq::worker::extract(app, &user, params, cancel).await,
        "qq_index" => {
            crate::qq::vector::index(app, &user, params["id"].as_str().unwrap_or("")).await?;
            Ok(json!({"indexed":true}))
        }
        "notify" => {
            notify(
                app,
                &user,
                params["title"].as_str().unwrap_or("提醒"),
                params["body"].as_str().unwrap_or(""),
                &job.id,
            )
            .await?;
            Ok(json!({"notified":true}))
        }
        "reminders" => {
            let rows:Vec<(String,String,i64)>=sqlx::query_as("SELECT id,title,due_at FROM records WHERE user_id=? AND deleted_at IS NULL AND status='open' AND kind IN ('tasks','events') AND due_at BETWEEN ? AND ?")
                .bind(&user.id).bind(now()-300).bind(now()+1800).fetch_all(&app.pool).await?;
            for (id, title, due) in &rows {
                notify(
                    app,
                    &user,
                    "日程与待办提醒",
                    title,
                    &format!("due:{id}:{due}"),
                )
                .await?;
            }
            Ok(json!({"reminded":rows.len()}))
        }
        "consolidate" => {
            let profile = crate::settings::active_profile(app, &user, None).await?;
            let ids: Vec<String> = sqlx::query_scalar(
                "SELECT id FROM conversations WHERE user_id=? ORDER BY updated_at DESC LIMIT 100",
            )
            .bind(&user.id)
            .fetch_all(&app.pool)
            .await?;
            let mut compacted = 0;
            for cid in ids {
                match crate::chat::Lease::acquire(app, &user, &cid).await {
                    Ok(lease) => {
                        if crate::chat::compact(app, &user, &cid, &profile, &lease, false, cancel)
                            .await?
                        {
                            compacted += 1;
                        }
                    }
                    Err(Error::Conflict(_)) => continue,
                    Err(e) => return Err(e),
                }
            }
            let memories = crate::memory::extract(app, &user, &profile, cancel).await?;
            Ok(json!({"compacted":compacted,"memories":memories}))
        }
        "briefing" => {
            let profile = crate::settings::active_profile(app, &user, None).await?;
            let tasks = crate::records::list(
                app,
                &user,
                "tasks",
                &crate::records::List {
                    status: Some("open".into()),
                    ..Default::default()
                },
            )
            .await?;
            let events = crate::records::list(app, &user, "events", &Default::default()).await?;
            let body=crate::provider::complete(app,&profile,&format!("请根据这些数据写简洁工作简报，区分已发生与未来事项，不虚构。当前UTC时间{}，用户时区{}。任务：{tasks}。日程：{events}",chrono::Utc::now(),user.timezone),cancel).await?;
            notify(app, &user, "灵犀简报", &body, &job.id).await?;
            Ok(json!({"body":body}))
        }
        "research" => {
            let prompt = params["prompt"]
                .as_str()
                .ok_or_else(|| invalid("prompt 必填"))?;
            let conv = crate::chat::create(app, &user, "后台调查", None).await?;
            let session = crate::chat::prepare(
                app,
                &user,
                &conv.id,
                crate::chat::Send {
                    message: prompt.into(),
                    request_id: format!("job:{}", job.id),
                },
            )
            .await?;
            let reply = crate::chat::run(app.clone(), session, None, cancel.clone()).await?;
            notify(app, &user, "后台调查完成", &reply, &job.id).await?;
            Ok(json!({"conversation_id":conv.id,"reply":reply}))
        }
        "image" => crate::integrations::generate_image(app, &user, params, cancel).await,
        "download" => crate::media::download(app, &user, params, &job.id, cancel).await,
        "webhook" => crate::integrations::send_webhook(app, &user, params).await,
        "skill" => {
            crate::skills::run(
                app,
                &user,
                params["id"]
                    .as_str()
                    .ok_or_else(|| invalid("技能 ID 必填"))?,
                cancel,
            )
            .await
        }
        "index" => {
            crate::search::index(
                app,
                &user,
                params["kind"].as_str().unwrap_or(""),
                params["id"].as_str().unwrap_or(""),
            )
            .await?;
            Ok(json!({"indexed":true}))
        }
        _ => Err(invalid("未知任务动作")),
    }
}
