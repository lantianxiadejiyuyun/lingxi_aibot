//! Private Feishu inbox. The official SDK handles the authenticated WebSocket;
//! this module owns identity, durable receipts, model execution and delivery.
use crate::{
    auth::{self, User},
    chat,
    db::{digest, id, now},
    error::{Error, Result, invalid},
    state::App,
};
use axum::{
    Json,
    extract::{FromRequestParts, State},
    http::request::Parts,
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::time::Duration;
use tokio_util::sync::CancellationToken;

const ORIGIN: &str = "https://open.feishu.cn/open-apis";
fn ok(v: Value) -> Json<Value> {
    Json(json!({"ok":true,"data":v}))
}
async fn ensure(app: &App, user: &User) -> Result<()> {
    sqlx::query("INSERT OR IGNORE INTO feishu_state(user_id) VALUES(?)")
        .bind(&user.id)
        .execute(&app.pool)
        .await?;
    Ok(())
}
async fn credentials(app: &App, user: &User) -> Result<Value> {
    let cfg = crate::integrations::config(app, user).await?;
    let c = cfg["channels"]["feishu_app"].clone();
    if ["app_id", "secret"]
        .iter()
        .any(|k| !c[k].as_str().is_some_and(|s| !s.trim().is_empty()))
    {
        return Err(invalid("请先在通知设置填写飞书应用 App ID 和 App Secret"));
    }
    Ok(c)
}
fn app_key(c: &Value) -> String {
    digest(&json!([c["app_id"], c["secret"]]).to_string())
}
async fn config_key(app: &App, user: &User, c: &Value) -> Result<String> {
    let revision: i64 = sqlx::query_scalar("SELECT revision FROM feishu_state WHERE user_id=?")
        .bind(&user.id)
        .fetch_one(&app.pool)
        .await?;
    Ok(digest(&format!("{}:{revision}", app_key(c))))
}
pub struct Bridge(pub User);
impl FromRequestParts<App> for Bridge {
    type Rejection = Error;
    async fn from_request_parts(parts: &mut Parts, app: &App) -> Result<Self> {
        let token = parts
            .headers
            .get("authorization")
            .and_then(|h| h.to_str().ok())
            .and_then(|s| s.strip_prefix("Bearer "))
            .filter(|s| s.len() == 64)
            .ok_or(Error::Unauthorized)?;
        // Managed credentials never replace the explicitly issued external token.
        // They are valid only while embedded mode and this account are enabled.
        let uid: Option<String> = sqlx::query_scalar(
            "SELECT user_id FROM feishu_state WHERE token_hash=? OR (? AND enabled=1 AND managed_token_hash=?)",
        )
        .bind(digest(token))
        .bind(app.feishu_embedded)
        .bind(digest(token))
        .fetch_optional(&app.pool)
        .await?;
        Ok(Self(
            auth::user(app, &uid.ok_or(Error::Unauthorized)?).await?,
        ))
    }
}
pub async fn view(State(app): State<App>, user: User) -> Result<Json<Value>> {
    ensure(&app, &user).await?;
    let (enabled,owner,chat,binding,heartbeat,state,version,spool,profile,cid):(bool,String,String,String,Option<i64>,String,String,i64,Option<String>,Option<String>)=sqlx::query_as("SELECT enabled,owner_id,chat_id,binding_key,heartbeat_at,connection_state,sdk_version,spool_count,profile_id,conversation_id FROM feishu_state WHERE user_id=?").bind(&user.id).fetch_one(&app.pool).await?;
    let c = credentials(&app, &user).await.ok();
    let bound = !owner.is_empty() && c.as_ref().is_some_and(|c| app_key(c) == binding);
    let visible_state = if !enabled {
        "disabled"
    } else if heartbeat.is_some_and(|t| now() - t < 50)
        || (app.feishu_embedded && state == "failed")
    {
        state.as_str()
    } else {
        "offline"
    };
    let recent:Vec<(i64,String,String,i64)>=sqlx::query_as("SELECT id,status,error,created_at FROM feishu_events WHERE user_id=? ORDER BY id DESC LIMIT 10").bind(&user.id).fetch_all(&app.pool).await?;
    Ok(ok(
        json!({"enabled":enabled,"managed":app.feishu_embedded,"configured":c.is_some(),"bound":bound,"owner_id":if bound {&owner}else{""},"chat_id":if bound {&chat}else{""},"conversation_id":cid,"profile_id":profile,"state":visible_state,"heartbeat_at":heartbeat,"sdk_version":version,"spool_count":spool,"events":recent.into_iter().map(|(id,status,error,created_at)|json!({"id":id,"status":status,"error":error,"created_at":created_at})).collect::<Vec<_>>()}),
    ))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ConfigInput {
    enabled: bool,
    profile_id: Option<String>,
}
pub async fn save(
    State(app): State<App>,
    user: User,
    Json(input): Json<ConfigInput>,
) -> Result<Json<Value>> {
    ensure(&app, &user).await?;
    if input.enabled {
        credentials(&app, &user).await?;
    }
    if let Some(p) = &input.profile_id {
        crate::settings::active_profile(&app, &user, Some(p)).await?;
    }
    sqlx::query("UPDATE feishu_state SET enabled=?,profile_id=?,revision=revision+1,pair_hash=NULL,pair_key=NULL,pair_expires_at=NULL WHERE user_id=?").bind(input.enabled).bind(&input.profile_id).bind(&user.id).execute(&app.pool).await?;
    Ok(ok(json!({"saved":true})))
}
pub async fn issue_bridge(State(app): State<App>, user: User) -> Result<Json<Value>> {
    ensure(&app, &user).await?;
    let token = format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    );
    sqlx::query("UPDATE feishu_state SET token_hash=? WHERE user_id=?")
        .bind(digest(&token))
        .bind(&user.id)
        .execute(&app.pool)
        .await?;
    Ok(ok(json!({"token":token})))
}
pub async fn pair(State(app): State<App>, user: User) -> Result<Json<Value>> {
    ensure(&app, &user).await?;
    let c = credentials(&app, &user).await?;
    let code = uuid::Uuid::new_v4().simple().to_string();
    sqlx::query("UPDATE feishu_state SET pair_hash=?,pair_key=?,pair_expires_at=? WHERE user_id=?")
        .bind(digest(&code))
        .bind(app_key(&c))
        .bind(now() + 600)
        .bind(&user.id)
        .execute(&app.pool)
        .await?;
    Ok(ok(
        json!({"command":format!("/绑定 {code}"),"expires_at":now()+600}),
    ))
}
pub async fn unbind(State(app): State<App>, user: User) -> Result<Json<Value>> {
    sqlx::query("UPDATE feishu_state SET owner_id='',chat_id='',binding_key='',conversation_id=NULL,revision=revision+1,pair_hash=NULL,pair_key=NULL,pair_expires_at=NULL WHERE user_id=?").bind(&user.id).execute(&app.pool).await?;
    Ok(ok(json!({"unbound":true})))
}
pub async fn bridge_config(State(app): State<App>, Bridge(user): Bridge) -> Result<Json<Value>> {
    let enabled: bool = sqlx::query_scalar("SELECT enabled FROM feishu_state WHERE user_id=?")
        .bind(&user.id)
        .fetch_one(&app.pool)
        .await?;
    let c = credentials(&app, &user).await.ok();
    if !enabled || c.is_none() {
        return Ok(ok(json!({"enabled":false})));
    }
    let c = c.unwrap();
    Ok(ok(
        json!({"enabled":true,"app_id":c["app_id"],"app_secret":c["secret"],"config_key":config_key(&app,&user,&c).await?}),
    ))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Heartbeat {
    state: String,
    sdk_version: String,
    spool_count: u32,
}
pub async fn heartbeat(
    State(app): State<App>,
    Bridge(user): Bridge,
    Json(v): Json<Heartbeat>,
) -> Result<Json<Value>> {
    if ![
        "idle",
        "connecting",
        "connected",
        "reconnecting",
        "failed",
        "disabled",
        "backend_unavailable",
    ]
    .contains(&v.state.as_str())
        || v.sdk_version.len() > 40
        || v.spool_count > 10000
    {
        return Err(invalid("状态无效"));
    }
    sqlx::query("UPDATE feishu_state SET heartbeat_at=?,connection_state=?,sdk_version=?,spool_count=? WHERE user_id=?").bind(now()).bind(v.state).bind(v.sdk_version).bind(v.spool_count).bind(&user.id).execute(&app.pool).await?;
    Ok(ok(json!({"received":true})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Incoming {
    pub config_key: String,
    pub message_id: String,
    pub sender_id: String,
    pub chat_id: String,
    pub chat_type: String,
    pub sender_type: String,
    pub message_type: String,
    pub text: String,
    pub created_at: i64,
}
pub async fn inbound(
    State(app): State<App>,
    Bridge(user): Bridge,
    Json(v): Json<Incoming>,
) -> Result<Json<Value>> {
    Ok(ok(accept(&app, &user, v).await?))
}
pub async fn accept(app: &App, user: &User, v: Incoming) -> Result<Value> {
    if [&v.message_id, &v.sender_id, &v.chat_id]
        .iter()
        .any(|s| s.is_empty() || s.len() > 128)
        || v.config_key.len() != 64
        || v.text.len() > 32000
    {
        return Err(invalid("飞书消息格式无效"));
    }
    let created_at = if v.created_at > 1_000_000_000_000 {
        v.created_at / 1000
    } else {
        v.created_at
    };
    if v.chat_type != "p2p"
        || v.sender_type != "user"
        || created_at < now() - 86400
        || created_at > now() + 300
    {
        return Ok(json!({"ignored":true}));
    }
    let c = credentials(app, user).await?;
    if config_key(app, user, &c).await? != v.config_key {
        return Ok(json!({"ignored":true,"reason":"settings_changed"}));
    }
    let mut tx = app.pool.begin().await?;
    // Acquire the write lock before checking pairing or dedupe; duplicate callbacks
    // cannot consume one pairing code twice or enqueue the same message twice.
    sqlx::query("UPDATE feishu_state SET revision=revision WHERE user_id=?")
        .bind(&user.id)
        .execute(&mut *tx)
        .await?;
    let (enabled,revision,owner,chat,binding):(bool,i64,String,String,String)=sqlx::query_as("SELECT enabled,revision,owner_id,chat_id,binding_key FROM feishu_state WHERE user_id=?").bind(&user.id).fetch_one(&mut *tx).await?;
    if !enabled || digest(&format!("{}:{revision}", app_key(&c))) != v.config_key {
        return Ok(json!({"ignored":true}));
    }
    let exists: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM feishu_events WHERE user_id=? AND message_id=?)",
    )
    .bind(&user.id)
    .bind(&v.message_id)
    .fetch_one(&mut *tx)
    .await?;
    if exists {
        return Ok(json!({"duplicate":true}));
    }
    let kind = if let Some(code) = v.text.trim().strip_prefix("/绑定 ") {
        let n=sqlx::query("UPDATE feishu_state SET owner_id=?,chat_id=?,binding_key=?,conversation_id=NULL,pair_hash=NULL,pair_key=NULL,pair_expires_at=NULL WHERE user_id=? AND pair_hash=? AND pair_key=? AND pair_expires_at>?")
            .bind(&v.sender_id).bind(&v.chat_id).bind(app_key(&c)).bind(&user.id).bind(digest(code.trim())).bind(app_key(&c)).bind(now()).execute(&mut *tx).await?.rows_affected();
        if n == 0 {
            return Ok(json!({"ignored":true}));
        }
        "binding"
    } else {
        if owner != v.sender_id || chat != v.chat_id || binding != app_key(&c) {
            return Ok(json!({"ignored":true,"reason":"unbound"}));
        }
        if v.message_type == "text" && !v.text.trim().is_empty() {
            "chat"
        } else {
            "unsupported"
        }
    };
    let pending:i64=sqlx::query_scalar("SELECT count(*) FROM feishu_events WHERE user_id=? AND status IN ('pending','queued','processing')").bind(&user.id).fetch_one(&mut *tx).await?;
    if pending >= 100 {
        return Err(Error::Busy);
    }
    sqlx::query("INSERT INTO feishu_events(user_id,message_id,config_key,owner_id,chat_id,text,kind,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)")
        .bind(&user.id).bind(v.message_id).bind(v.config_key).bind(v.sender_id).bind(v.chat_id).bind(if kind=="chat" {v.text}else{String::new()}).bind(kind).bind(now()).bind(now()).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(json!({"accepted":true,"bound":kind=="binding"}))
}
/// Schedule one message per account at a time; recovery never repeats an
/// uncertain external send. Every intent is committed before making HTTP calls.
pub async fn reconcile(app: &App) -> Result<()> {
    let mut tx = app.pool.begin().await?;
    sqlx::query("UPDATE feishu_events SET status='unknown',error='处理曾中断，请核对会话后重新发送',updated_at=? WHERE status IN ('queued','processing') AND (job_id IS NULL OR EXISTS(SELECT 1 FROM jobs WHERE jobs.id=feishu_events.job_id AND jobs.status IN ('failed','cancelled','needs_review','completed')))").bind(now()).execute(&mut *tx).await?;
    let rows:Vec<(i64,String)>=sqlx::query_as("SELECT e.id,e.user_id FROM feishu_events e WHERE e.status='pending' AND e.id=(SELECT min(p.id) FROM feishu_events p WHERE p.user_id=e.user_id AND p.status='pending') AND NOT EXISTS(SELECT 1 FROM feishu_events r WHERE r.user_id=e.user_id AND r.status IN ('queued','processing')) LIMIT 16").fetch_all(&mut *tx).await?;
    for (eid, uid) in rows {
        let jid = id();
        sqlx::query("INSERT INTO jobs(id,user_id,action,params,run_at,max_attempts,created_at,updated_at) VALUES(?,?,'feishu_message',?,?,1,?,?)").bind(&jid).bind(uid).bind(json!({"id":eid}).to_string()).bind(now()).bind(now()).bind(now()).execute(&mut *tx).await?;
        sqlx::query("UPDATE feishu_events SET status='queued',job_id=?,updated_at=? WHERE id=?")
            .bind(jid)
            .bind(now())
            .bind(eid)
            .execute(&mut *tx)
            .await?;
    }
    tx.commit().await?;
    Ok(())
}
async fn permitted(app: &App, user: &User, eid: i64) -> Result<bool> {
    let kind: Option<String> =
        sqlx::query_scalar("SELECT kind FROM feishu_events WHERE id=? AND user_id=?")
            .bind(eid)
            .bind(&user.id)
            .fetch_optional(&app.pool)
            .await?;
    if kind.as_deref() != Some("binding")
        && !crate::permissions::allowed(
            app,
            user,
            crate::permissions::Feature::Chat,
            crate::permissions::Scope::Private,
        )
        .await?
    {
        return Ok(false);
    }
    let c = credentials(app, user).await?;
    let key = config_key(app, user, &c).await?;
    Ok(sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM feishu_events e JOIN feishu_state s ON s.user_id=e.user_id WHERE e.id=? AND e.user_id=? AND s.enabled=1 AND e.owner_id=s.owner_id AND e.chat_id=s.chat_id AND s.binding_key=? AND e.config_key=?)").bind(eid).bind(&user.id).bind(app_key(&c)).bind(key).fetch_one(&app.pool).await?)
}
pub async fn process(
    app: &App,
    user: &User,
    params: &Value,
    cancel: &CancellationToken,
) -> Result<Value> {
    process_at(app, user, params, cancel, ORIGIN).await
}
async fn process_at(
    app: &App,
    user: &User,
    params: &Value,
    cancel: &CancellationToken,
    origin: &str,
) -> Result<Value> {
    let eid = params["id"]
        .as_i64()
        .ok_or_else(|| invalid("消息编号无效"))?;
    let n=sqlx::query("UPDATE feishu_events SET status='processing',updated_at=? WHERE id=? AND user_id=? AND status='queued'").bind(now()).bind(eid).bind(&user.id).execute(&app.pool).await?.rows_affected();
    if n == 0 {
        return Ok(json!({"skipped":true}));
    }
    let outcome = process_inner(app, user, eid, cancel, origin).await;
    let (status, error) = match &outcome {
        Ok(true) => ("sent", ""),
        Ok(false) => ("cancelled", "设置或绑定已改变"),
        Err(_) => ("failed", "处理或投递未完成；请核对会话及权限，不自动重发"),
    };
    sqlx::query("UPDATE feishu_events SET status=?,error=?,updated_at=? WHERE id=?")
        .bind(status)
        .bind(error)
        .bind(now())
        .bind(eid)
        .execute(&app.pool)
        .await?;
    outcome.map(|sent| json!({"sent":sent}))
}
async fn process_inner(
    app: &App,
    user: &User,
    eid: i64,
    cancel: &CancellationToken,
    origin: &str,
) -> Result<bool> {
    if cancel.is_cancelled() || !permitted(app, user, eid).await? {
        return Ok(false);
    }
    let style = crate::qq::config(app, user).await?.reply_style;
    let (text, kind, chat_id): (String, String, String) =
        sqlx::query_as("SELECT text,kind,chat_id FROM feishu_events WHERE id=? AND user_id=?")
            .bind(eid)
            .bind(&user.id)
            .fetch_one(&app.pool)
            .await?;
    let reply = match kind.as_str() {
        "binding" => "飞书私聊已绑定。接下来直接给我发文字，就可以继续聊天了。".into(),
        "unsupported" => "目前飞书私聊支持文字消息。请把想聊的内容发成文字。".into(),
        _ => {
            let (cid, profile): (Option<String>, Option<String>) = sqlx::query_as(
                "SELECT conversation_id,profile_id FROM feishu_state WHERE user_id=?",
            )
            .bind(&user.id)
            .fetch_one(&app.pool)
            .await?;
            let cid = if let Some(cid) = cid {
                cid
            } else {
                let conv = chat::create(app, user, "飞书私聊", profile.clone()).await?;
                sqlx::query("UPDATE feishu_state SET conversation_id=? WHERE user_id=?")
                    .bind(&conv.id)
                    .bind(&user.id)
                    .execute(&app.pool)
                    .await?;
                conv.id
            };
            sqlx::query("UPDATE conversations SET profile_id=? WHERE id=? AND user_id=?")
                .bind(profile)
                .bind(&cid)
                .bind(&user.id)
                .execute(&app.pool)
                .await?;
            let mut session = chat::prepare(
                app,
                user,
                &cid,
                chat::Send {
                    message: text,
                    request_id: format!("feishu:{eid}"),
                },
            )
            .await?;
            session
                .profile
                .system_prompt
                .push_str(&crate::qq::dialogue::prompt(&style));
            chat::run(app.clone(), session, None, cancel.clone()).await?
        }
    };
    sqlx::query("UPDATE feishu_events SET reply=? WHERE id=?")
        .bind(&reply)
        .bind(eid)
        .execute(&app.pool)
        .await?;
    if reply.trim().is_empty() {
        return Err(invalid("模型未生成回复"));
    }
    let parts: Vec<String> = crate::qq::dialogue::split(&reply, &style)
        .into_iter()
        .flat_map(|p| split_text(&p))
        .collect();
    for (i, body) in parts.iter().enumerate() {
        sqlx::query("INSERT INTO feishu_parts(event_id,part,body) VALUES(?,?,?)")
            .bind(eid)
            .bind(i as i64)
            .bind(body)
            .execute(&app.pool)
            .await?;
    }
    let c = credentials(app, user).await?;
    let token = tenant_token(app, &c, origin).await?;
    for (i, body) in parts.iter().enumerate() {
        if i > 0 && style.enabled {
            tokio::select! {
                _=cancel.cancelled()=>return Ok(false),
                _=tokio::time::sleep(Duration::from_secs(crate::qq::dialogue::part_delay_secs()))=>(),
            }
        }
        if cancel.is_cancelled() || !permitted(app, user, eid).await? {
            return Ok(false);
        }
        sqlx::query("UPDATE feishu_parts SET status='sending' WHERE event_id=? AND part=?")
            .bind(eid)
            .bind(i as i64)
            .execute(&app.pool)
            .await?;
        let sent = send_text(app, &token, origin, "chat_id", &chat_id, body).await;
        match sent {
            Ok(receipt) => {
                sqlx::query(
                    "UPDATE feishu_parts SET status='sent',receipt=? WHERE event_id=? AND part=?",
                )
                .bind(receipt)
                .bind(eid)
                .bind(i as i64)
                .execute(&app.pool)
                .await?;
            }
            Err(e) => {
                sqlx::query("UPDATE feishu_parts SET status='unknown' WHERE event_id=? AND part=?")
                    .bind(eid)
                    .bind(i as i64)
                    .execute(&app.pool)
                    .await?;
                return Err(e);
            }
        }
    }
    Ok(true)
}
pub fn split_text(text: &str) -> Vec<String> {
    let mut result = vec![];
    let mut part = String::new();
    for ch in text.chars() {
        if part.len() + ch.len_utf8() > 3000 {
            result.push(std::mem::take(&mut part));
        }
        part.push(ch);
    }
    if !part.is_empty() {
        result.push(part);
    }
    result
}
async fn response(req: reqwest::RequestBuilder) -> Result<Value> {
    let r = req
        .timeout(Duration::from_secs(20))
        .send()
        .await
        .map_err(|_| Error::Provider("飞书请求未完成，投递结果需核实".into()))?;
    let v = crate::integrations::json_response(r).await?;
    if v["code"] != 0 {
        return Err(Error::Provider(format!(
            "飞书返回错误码 {}，请检查应用权限与发布状态",
            v["code"].as_i64().unwrap_or(-1)
        )));
    }
    Ok(v)
}
pub async fn tenant_token(app: &App, c: &Value, origin: &str) -> Result<String> {
    let v = response(
        app.http
            .post(format!("{origin}/auth/v3/tenant_access_token/internal"))
            .json(&json!({"app_id":c["app_id"],"app_secret":c["secret"]})),
    )
    .await?;
    v["tenant_access_token"]
        .as_str()
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| invalid("飞书未返回访问凭证"))
}
pub async fn send_text(
    app: &App,
    token: &str,
    origin: &str,
    kind: &str,
    recipient: &str,
    body: &str,
) -> Result<String> {
    if !["open_id", "chat_id", "user_id", "union_id"].contains(&kind) {
        return Err(invalid("飞书接收人类型无效"));
    }
    let v=response(app.http.post(format!("{origin}/im/v1/messages")).query(&[("receive_id_type",kind)]).bearer_auth(token).json(&json!({"receive_id":recipient,"msg_type":"text","content":json!({"text":body}).to_string()}))).await?;
    v["data"]["message_id"]
        .as_str()
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| invalid("飞书未返回消息回执，结果需核实"))
}
pub async fn diagnose(State(app): State<App>, user: User) -> Result<Json<Value>> {
    let c = credentials(&app, &user).await?;
    let token = tenant_token(&app, &c, ORIGIN).await?;
    let v = response(
        app.http
            .get(format!("{ORIGIN}/bot/v3/info"))
            .bearer_auth(token),
    )
    .await?;
    Ok(ok(
        json!({"authenticated":true,"bot_enabled":v["bot"]["activate_status"]==2,"sent_messages":0}),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        Router,
        body::Body,
        http::{Request, StatusCode},
        routing::post,
    };
    use http_body_util::BodyExt;
    use std::sync::{Arc, Mutex};
    use tower::ServiceExt;
    #[derive(Clone, Default)]
    struct Mock {
        models: Arc<Mutex<Vec<Value>>>,
        sends: Arc<Mutex<Vec<Value>>>,
        response: Arc<Mutex<Value>>,
        model_text: Arc<Mutex<Option<String>>>,
    }
    struct Fixture {
        app: App,
        user: User,
        m: Mock,
        origin: String,
        server: tokio::task::JoinHandle<()>,
        _temp: tempfile::TempDir,
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            self.server.abort();
        }
    }
    impl Fixture {
        async fn new() -> Self {
            let temp = tempfile::tempdir().unwrap();
            let app = App::new(
                crate::db::connect(&temp.path().join("main.db"))
                    .await
                    .unwrap(),
                crate::db::Secrets::from_key(&[7; 32]),
                temp.path().into(),
                true,
            )
            .unwrap();
            let user = auth::create_user(&app, "owner", "feishu-test-password".into(), false)
                .await
                .unwrap();
            let m = Mock::default();
            *m.response.lock().unwrap() = json!({"code":0,"data":{"message_id":"om_receipt"}});
            let router = Router::new()
                .route(
                    "/auth/v3/tenant_access_token/internal",
                    post(|| async { Json(json!({"code":0,"tenant_access_token":"test-token"})) }),
                )
                .route(
                    "/im/v1/messages",
                    post(|State(m): State<Mock>, Json(v): Json<Value>| async move {
                        m.sends.lock().unwrap().push(v);
                        Json(m.response.lock().unwrap().clone())
                    }),
                )
                .route(
                    "/v1/chat/completions",
                    post(|State(m): State<Mock>, Json(v): Json<Value>| async move {
                        m.models.lock().unwrap().push(v);
                        (
                            [("content-type", "text/event-stream")],
                            format!(
                                "data: {}\n\ndata: [DONE]\n\n",
                                json!({"choices":[{"delta":{"content":m.model_text.lock().unwrap().clone().unwrap_or_else(||"你好，这是回复。".into())}}]})
                            ),
                        )
                    }),
                )
                .with_state(m.clone());
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let origin = format!("http://{}", listener.local_addr().unwrap());
            let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
            crate::settings::save_profiles(
                &app,
                &user,
                vec![crate::settings::Profile {
                    id: "model".into(),
                    name: "mock".into(),
                    protocol: "openai".into(),
                    base_url: format!("{origin}/v1"),
                    api_key: "test".into(),
                    model: "mock".into(),
                    context_window: 16000,
                    max_output: 1000,
                    system_prompt: String::new(),
                }],
            )
            .await
            .unwrap();
            crate::integrations::save_config(
                &app,
                &user,
                json!({"channels":{"feishu_app":{"app_id":"cli_test","secret":"app-secret"}}}),
            )
            .await
            .unwrap();
            let _ = save(
                State(app.clone()),
                user.clone(),
                Json(ConfigInput {
                    enabled: true,
                    profile_id: None,
                }),
            )
            .await
            .unwrap();
            Self {
                app,
                user,
                m,
                origin,
                server,
                _temp: temp,
            }
        }
        async fn event(&self, mid: &str, text: &str) -> Incoming {
            let c = credentials(&self.app, &self.user).await.unwrap();
            Incoming {
                config_key: config_key(&self.app, &self.user, &c).await.unwrap(),
                message_id: mid.into(),
                sender_id: "ou_owner".into(),
                chat_id: "oc_private".into(),
                chat_type: "p2p".into(),
                sender_type: "user".into(),
                message_type: "text".into(),
                text: text.into(),
                created_at: now(),
            }
        }
        async fn bind(&self) {
            let v = pair(State(self.app.clone()), self.user.clone())
                .await
                .unwrap()
                .0;
            let command = v["data"]["command"].as_str().unwrap();
            assert_eq!(
                accept(&self.app, &self.user, self.event("binding", command).await)
                    .await
                    .unwrap()["bound"],
                true
            );
            sqlx::query("UPDATE feishu_events SET status='sent' WHERE kind='binding'")
                .execute(&self.app.pool)
                .await
                .unwrap();
        }
        async fn latest(&self) -> i64 {
            sqlx::query_scalar("SELECT max(id) FROM feishu_events")
                .fetch_one(&self.app.pool)
                .await
                .unwrap()
        }
        async fn process(&self, eid: i64) -> Result<Value> {
            process_at(
                &self.app,
                &self.user,
                &json!({"id":eid}),
                &CancellationToken::new(),
                &self.origin,
            )
            .await
        }
        async fn request(&self, path: &str, token: &str) -> (StatusCode, Value) {
            let r = crate::api::router(self.app.clone())
                .oneshot(
                    Request::builder()
                        .uri(path)
                        .header("authorization", format!("Bearer {token}"))
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            let status = r.status();
            let v =
                serde_json::from_slice(&r.into_body().collect().await.unwrap().to_bytes()).unwrap();
            (status, v)
        }
    }
    #[tokio::test]
    async fn bridge_token_is_scoped_and_rotatable() {
        let f = Fixture::new().await;
        let token = issue_bridge(State(f.app.clone()), f.user.clone())
            .await
            .unwrap()
            .0["data"]["token"]
            .as_str()
            .unwrap()
            .to_owned();
        assert_eq!(
            f.request("/api/v2/feishu/bridge/config", &token).await.0,
            StatusCode::OK
        );
        assert_eq!(
            f.request("/api/v2/memories", &token).await.0,
            StatusCode::UNAUTHORIZED
        );
        let normal = auth::issue_token(&f.app, &f.user, "test", 3600)
            .await
            .unwrap();
        assert_eq!(
            f.request("/api/v2/feishu/bridge/config", &normal).await.0,
            StatusCode::UNAUTHORIZED
        );
        let (_, v) = f.request("/api/v2/feishu", &normal).await;
        assert!(!v.to_string().contains("app-secret"));
        let _ = issue_bridge(State(f.app.clone()), f.user.clone())
            .await
            .unwrap();
        assert_eq!(
            f.request("/api/v2/feishu/bridge/config", &token).await.0,
            StatusCode::UNAUTHORIZED
        );
    }
    #[tokio::test]
    async fn managed_tokens_are_separate_scoped_and_follow_enabled_state() {
        let mut f = Fixture::new().await;
        let external = issue_bridge(State(f.app.clone()), f.user.clone())
            .await
            .unwrap()
            .0["data"]["token"]
            .as_str()
            .unwrap()
            .to_owned();
        let managed = "c".repeat(64);
        sqlx::query("UPDATE feishu_state SET managed_token_hash=? WHERE user_id=?")
            .bind(digest(&managed))
            .bind(&f.user.id)
            .execute(&f.app.pool)
            .await
            .unwrap();
        assert_eq!(
            f.request("/api/v2/feishu/bridge/config", &managed).await.0,
            StatusCode::UNAUTHORIZED
        );
        f.app.feishu_embedded = true;
        assert_eq!(
            f.request("/api/v2/feishu/bridge/config", &managed).await.0,
            StatusCode::OK
        );
        assert_eq!(
            f.request("/api/v2/memories", &managed).await.0,
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            f.request("/api/v2/feishu/bridge/config", &external).await.0,
            StatusCode::OK
        );
        let _ = issue_bridge(State(f.app.clone()), f.user.clone())
            .await
            .unwrap();
        assert_eq!(
            f.request("/api/v2/feishu/bridge/config", &managed).await.0,
            StatusCode::OK
        );

        let other = auth::create_user(
            &f.app,
            "another-owner",
            "feishu-other-password".into(),
            false,
        )
        .await
        .unwrap();
        ensure(&f.app, &other).await.unwrap();
        let other_token = issue_bridge(State(f.app.clone()), other.clone())
            .await
            .unwrap()
            .0["data"]["token"]
            .as_str()
            .unwrap()
            .to_owned();
        let (_, other_config) = f
            .request("/api/v2/feishu/bridge/config", &other_token)
            .await;
        assert_eq!(other_config["data"]["enabled"], false);
        let (_, own_config) = f.request("/api/v2/feishu/bridge/config", &managed).await;
        assert_eq!(own_config["data"]["app_id"], "cli_test");
        let normal = auth::issue_token(&f.app, &f.user, "test", 3600)
            .await
            .unwrap();
        let (_, status) = f.request("/api/v2/feishu", &normal).await;
        assert_eq!(status["data"]["managed"], true);
        assert!(!status.to_string().contains(&managed));

        sqlx::query("UPDATE feishu_state SET enabled=0 WHERE user_id=?")
            .bind(&f.user.id)
            .execute(&f.app.pool)
            .await
            .unwrap();
        assert_eq!(
            f.request("/api/v2/feishu/bridge/config", &managed).await.0,
            StatusCode::UNAUTHORIZED
        );
        sqlx::query("UPDATE feishu_state SET enabled=1 WHERE user_id=?")
            .bind(&f.user.id)
            .execute(&f.app.pool)
            .await
            .unwrap();
        sqlx::query("UPDATE users SET disabled=1 WHERE id=?")
            .bind(&f.user.id)
            .execute(&f.app.pool)
            .await
            .unwrap();
        assert_ne!(
            f.request("/api/v2/feishu/bridge/config", &managed).await.0,
            StatusCode::OK
        );
    }
    #[tokio::test]
    async fn identity_pairing_is_single_use_and_untrusted_events_cannot_chat() {
        let f = Fixture::new().await;
        assert_eq!(
            accept(&f.app, &f.user, f.event("unbound", "hello").await)
                .await
                .unwrap()["ignored"],
            true
        );
        let p = pair(State(f.app.clone()), f.user.clone()).await.unwrap().0;
        let cmd = p["data"]["command"].as_str().unwrap();
        let mut group = f.event("group", cmd).await;
        group.chat_type = "group".into();
        assert_eq!(
            accept(&f.app, &f.user, group).await.unwrap()["ignored"],
            true
        );
        assert_eq!(
            accept(&f.app, &f.user, f.event("bind", cmd).await)
                .await
                .unwrap()["bound"],
            true
        );
        assert_eq!(
            accept(&f.app, &f.user, f.event("bind", cmd).await)
                .await
                .unwrap()["duplicate"],
            true
        );
        let mut stranger = f.event("stranger", cmd).await;
        stranger.sender_id = "ou_other".into();
        assert_eq!(
            accept(&f.app, &f.user, stranger).await.unwrap()["ignored"],
            true
        );
        let mut wrong_chat = f.event("wrong-chat", "hi").await;
        wrong_chat.chat_id = "oc_other".into();
        assert_eq!(
            accept(&f.app, &f.user, wrong_chat).await.unwrap()["ignored"],
            true
        );
        let mut bot = f.event("bot", "hi").await;
        bot.sender_type = "app".into();
        assert_eq!(accept(&f.app, &f.user, bot).await.unwrap()["ignored"], true);
        let text: String =
            sqlx::query_scalar("SELECT text FROM feishu_events WHERE message_id='bind'")
                .fetch_one(&f.app.pool)
                .await
                .unwrap();
        assert!(text.is_empty());
        assert!(f.m.models.lock().unwrap().is_empty());
    }
    #[tokio::test]
    async fn permissions_revoke_queued_feishu_reply_without_unbinding() {
        let f = Fixture::new().await;
        f.bind().await;
        accept(&f.app, &f.user, f.event("permission-message", "你好").await)
            .await
            .unwrap();
        let eid = f.latest().await;
        let mut cfg = crate::permissions::Config::default();
        cfg.features
            .get_mut(&crate::permissions::Feature::Chat)
            .unwrap()
            .private = false;
        crate::settings::put(&f.app, &f.user, "feature_permissions", &json!(cfg))
            .await
            .unwrap();
        f.process(eid).await.unwrap();
        assert!(f.m.models.lock().unwrap().is_empty());
        assert!(!permitted(&f.app, &f.user, eid).await.unwrap());
        cfg.features
            .get_mut(&crate::permissions::Feature::Chat)
            .unwrap()
            .private = true;
        crate::settings::put(&f.app, &f.user, "feature_permissions", &json!(cfg))
            .await
            .unwrap();
        assert!(permitted(&f.app, &f.user, eid).await.unwrap());
    }
    #[tokio::test]
    async fn private_chat_is_durable_ordered_deduplicated_and_uses_main_history() {
        let f = Fixture::new().await;
        f.bind().await;
        accept(&f.app, &f.user, f.event("m1", "第一条").await)
            .await
            .unwrap();
        let first = f.latest().await;
        accept(&f.app, &f.user, f.event("m2", "第二条").await)
            .await
            .unwrap();
        let second = f.latest().await;
        reconcile(&f.app).await.unwrap();
        reconcile(&f.app).await.unwrap();
        let count: i64 =
            sqlx::query_scalar("SELECT count(*) FROM jobs WHERE action='feishu_message'")
                .fetch_one(&f.app.pool)
                .await
                .unwrap();
        assert_eq!(count, 1);
        assert_eq!(f.process(first).await.unwrap()["sent"], true);
        assert_eq!(
            accept(&f.app, &f.user, f.event("m1", "第一条").await)
                .await
                .unwrap()["duplicate"],
            true
        );
        assert_eq!(f.process(first).await.unwrap()["skipped"], true);
        reconcile(&f.app).await.unwrap();
        assert_eq!(f.process(second).await.unwrap()["sent"], true);
        assert_eq!(f.m.models.lock().unwrap().len(), 2);
        assert_eq!(f.m.sends.lock().unwrap().len(), 2);
        let cid: String = sqlx::query_scalar("SELECT conversation_id FROM feishu_state")
            .fetch_one(&f.app.pool)
            .await
            .unwrap();
        let history = chat::messages(&f.app, &f.user, &cid, 0, 100).await.unwrap();
        assert_eq!(history.len(), 4);
        assert_eq!(history[0].content, "第一条");
        assert_eq!(history[2].content, "第二条");
        let requests = f.m.models.lock().unwrap();
        assert!(requests[1].to_string().contains("第一条"));
        assert!(!requests[1].to_string().contains("大肥鱼"));
    }
    #[tokio::test]
    async fn paragraphs_are_sent_in_order_and_not_replayed() {
        let f = Fixture::new().await;
        f.bind().await;
        let mut cfg = crate::integrations::config(&f.app, &f.user).await.unwrap();
        cfg["qq"] = json!({"reply_style":{"enabled":true,"max_messages":3,"interval_secs":1}});
        crate::integrations::save_config(&f.app, &f.user, cfg)
            .await
            .unwrap();
        *f.m.model_text.lock().unwrap() = Some("第一段完整意思。\n\n第二段完整意思。".into());
        accept(&f.app, &f.user, f.event("paragraphs", "聊两句").await)
            .await
            .unwrap();
        let event = f.latest().await;
        reconcile(&f.app).await.unwrap();
        assert_eq!(f.process(event).await.unwrap()["sent"], true);
        assert_eq!(f.process(event).await.unwrap()["skipped"], true);
        let sends = f.m.sends.lock().unwrap();
        assert_eq!(sends.len(), 2);
        for (send, text) in sends.iter().zip(["第一段完整意思。", "第二段完整意思。"])
        {
            let payload: Value = serde_json::from_str(send["content"].as_str().unwrap()).unwrap();
            assert_eq!(payload["text"], text);
        }
        assert!(
            json!(*f.m.models.lock().unwrap())
                .to_string()
                .contains("每段单独发送")
        );
    }
    #[tokio::test]
    async fn unbind_and_changed_config_cancel_queued_work() {
        let f = Fixture::new().await;
        f.bind().await;
        let old = f.event("old", "hi").await;
        accept(&f.app, &f.user, f.event("queued", "hi").await)
            .await
            .unwrap();
        let eid = f.latest().await;
        reconcile(&f.app).await.unwrap();
        let _ = unbind(State(f.app.clone()), f.user.clone()).await.unwrap();
        assert_eq!(f.process(eid).await.unwrap()["sent"], false);
        assert_eq!(accept(&f.app, &f.user, old).await.unwrap()["ignored"], true);
        assert!(f.m.models.lock().unwrap().is_empty());
        assert!(f.m.sends.lock().unwrap().is_empty());
    }
    #[tokio::test]
    async fn expired_pairing_and_rotated_app_credentials_are_rejected() {
        let f = Fixture::new().await;
        let p = pair(State(f.app.clone()), f.user.clone()).await.unwrap().0;
        sqlx::query("UPDATE feishu_state SET pair_expires_at=0")
            .execute(&f.app.pool)
            .await
            .unwrap();
        assert_eq!(
            accept(
                &f.app,
                &f.user,
                f.event("expired", p["data"]["command"].as_str().unwrap())
                    .await
            )
            .await
            .unwrap()["ignored"],
            true
        );
        f.bind().await;
        let mut cfg = crate::integrations::config(&f.app, &f.user).await.unwrap();
        cfg["channels"]["feishu_app"]["secret"] = json!("new-secret");
        crate::integrations::save_config(&f.app, &f.user, cfg)
            .await
            .unwrap();
        assert_eq!(
            view(State(f.app.clone()), f.user.clone()).await.unwrap().0["data"]["bound"],
            false
        );
        assert_eq!(
            accept(&f.app, &f.user, f.event("new", "hi").await)
                .await
                .unwrap()["ignored"],
            true
        );
    }
    #[tokio::test]
    async fn failed_or_uncertain_send_is_not_replayed_and_does_not_block_next_message() {
        let f = Fixture::new().await;
        f.bind().await;
        accept(&f.app, &f.user, f.event("m1", "hi").await)
            .await
            .unwrap();
        let eid = f.latest().await;
        reconcile(&f.app).await.unwrap();
        *f.m.response.lock().unwrap() = json!({"code":0});
        assert!(f.process(eid).await.is_err());
        assert_eq!(f.process(eid).await.unwrap()["skipped"], true);
        let state: String = sqlx::query_scalar("SELECT status FROM feishu_parts WHERE event_id=?")
            .bind(eid)
            .fetch_one(&f.app.pool)
            .await
            .unwrap();
        assert_eq!(state, "unknown");
        assert_eq!(f.m.sends.lock().unwrap().len(), 1);
        accept(&f.app, &f.user, f.event("m2", "hi again").await)
            .await
            .unwrap();
        let next = f.latest().await;
        reconcile(&f.app).await.unwrap();
        sqlx::query("UPDATE jobs SET status='needs_review' WHERE id=(SELECT job_id FROM feishu_events WHERE id=?)").bind(next).execute(&f.app.pool).await.unwrap();
        reconcile(&f.app).await.unwrap();
        let state: String = sqlx::query_scalar("SELECT status FROM feishu_events WHERE id=?")
            .bind(next)
            .fetch_one(&f.app.pool)
            .await
            .unwrap();
        assert_eq!(state, "unknown");
        assert_eq!(f.m.models.lock().unwrap().len(), 1);
    }
    #[test]
    fn text_splitting_preserves_unicode_and_fits_json_payload_limits() {
        let text = format!("{}{}", "你好🐟\n".repeat(1200), "\u{0001}".repeat(3500));
        let parts = split_text(&text);
        assert_eq!(parts.concat(), text);
        assert!(parts.len() > 1);
        for part in parts {
            assert!(
                json!({"content":json!({"text":part}).to_string()})
                    .to_string()
                    .len()
                    < 20000
            );
        }
    }
}
