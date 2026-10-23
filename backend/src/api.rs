use crate::{
    auth::{self, User},
    chat,
    db::now,
    error::{Error, Result, invalid},
    records,
    state::App,
};
use axum::{
    Json, Router,
    extract::{
        DefaultBodyLimit, Path, Query, State,
        ws::{Message as WsMessage, WebSocket, WebSocketUpgrade},
    },
    http::{HeaderValue, StatusCode, header},
    response::{
        Html, IntoResponse, Response,
        sse::{Event, KeepAlive, Sse},
    },
    routing::{get, post},
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{convert::Infallible, time::Duration};
use tower_http::{set_header::SetResponseHeaderLayer, trace::TraceLayer};

pub fn router(app: App) -> Router {
    Router::new()
        .route("/healthz",get(||async{Json(json!({"ok":true,"service":"lingxi","version":env!("CARGO_PKG_VERSION")}))}))
        .route("/readyz",get(ready))
        .route("/",get(crate::frontend::index))
        .route("/assets/{*path}",get(crate::frontend::static_asset))
        .route("/api/v2/login",post(auth::login))
        .route("/api/v2/logout",post(auth::logout))
        .route("/api/v2/me",get(auth::me).patch(update_me))
        .route("/api/v2/deployment",get(crate::deployment::view))
        .route("/api/v2/deployment/napcat/configure",post(crate::deployment::configure_napcat))
        .route("/api/v2/deployment/napcat/login",post(crate::deployment::napcat_login))
        .route("/api/v2/users",get(users).post(create_user))
        .route("/api/v2/tokens",get(tokens).post(create_token))
        .route("/api/v2/tokens/{hash}",axum::routing::delete(revoke_token))
        .route("/api/v2/dashboard",get(dashboard))
        .route("/api/v2/records/{kind}",get(list_records).post(create_record))
        .route("/api/v2/records/{kind}/{id}",get(get_record).put(update_record).delete(delete_record))
        .route("/api/v2/search",get(search))
        .route("/api/v2/memories",get(memories).post(remember))
        .route("/api/v2/memories/{id}",axum::routing::delete(forget))
        .route("/api/v2/skills/{id}/run",post(run_skill))
        .route("/api/v2/profiles",get(profiles).put(save_profiles))
        .route("/api/v2/persona/{profile}",get(crate::growth::view).put(crate::growth::save))
        .route("/api/v2/persona/{profile}/traits",post(crate::growth::write_trait))
        .route("/api/v2/persona/{profile}/traits/{id}",axum::routing::delete(crate::growth::forget))
        .route("/api/v2/integrations",get(integrations).put(save_integrations))
        .route("/api/v2/conversations",get(conversations).post(create_conversation))
        .route("/api/v2/conversations/{id}",get(get_conversation).patch(update_conversation).delete(delete_conversation))
        .route("/api/v2/conversations/{id}/messages",get(messages).post(send))
        .route("/api/v2/conversations/{id}/compact",post(compact))
        .route("/api/v2/jobs",get(jobs).post(enqueue))
        .route("/api/v2/jobs/{id}",get(job))
        .route("/api/v2/jobs/{id}/cancel",post(cancel_job))
        .route("/api/v2/schedules",get(schedules).post(create_schedule))
        .route("/api/v2/schedules/{id}",axum::routing::delete(delete_schedule).patch(toggle_schedule))
        .route("/api/v2/notifications",get(notifications))
        .route("/api/v2/notifications/{id}/read",post(read_notification))
        .route("/api/v2/storage",get(storage))
        .route("/api/v2/storage/{id}/files",get(files))
        .route("/api/v2/assets/{id}",get(asset))
        .route("/api/v2/bridge/inbound",post(bridge))
        .route("/api/v2/feishu",get(crate::feishu::view).put(crate::feishu::save))
        .route("/api/v2/feishu/pair",post(crate::feishu::pair).delete(crate::feishu::unbind))
        .route("/api/v2/feishu/diagnose",post(crate::feishu::diagnose))
        .route("/api/v2/feishu/bridge-token",post(crate::feishu::issue_bridge))
        .route("/api/v2/feishu/bridge/config",get(crate::feishu::bridge_config))
        .route("/api/v2/feishu/bridge/heartbeat",post(crate::feishu::heartbeat))
        .route("/api/v2/feishu/bridge/inbound",post(crate::feishu::inbound))
        .route("/api/v2/companion",get(crate::companion::view).put(crate::companion::save_api))
        .route("/api/v2/bilibili",get(crate::bilibili::list).put(crate::bilibili::save_api))
        .route("/api/v2/bilibili/run",post(crate::bilibili::run_api))
        .route("/api/v2/bilibili/entries/{id}",get(crate::bilibili::detail).patch(crate::bilibili::mark).delete(crate::bilibili::delete))
        .route("/api/v2/bilibili/login",post(crate::bilibili::login_start).delete(crate::bilibili::disconnect))
        .route("/api/v2/bilibili/login/{id}",post(crate::bilibili::login_poll))
        .route("/api/v2/notification-channels",get(crate::companion::channels::settings_api).put(crate::companion::channels::save_api))
        .route("/api/v2/integrations/astrbot/binding",get(crate::companion::private::binding_api).put(crate::companion::private::bind_api).delete(crate::companion::private::unbind_api))
        .route("/api/v2/integrations/astrbot/chat",post(crate::companion::private::chat_api))
        .route("/api/v2/integrations/astrbot/outbox/claim",post(crate::companion::private::claim_api))
        .route("/api/v2/integrations/astrbot/outbox/{id}/ack",post(crate::companion::private::ack_api))
        .route("/api/v2/qq/inbound/{user_id}",post(crate::qq::api::inbound))
        .route("/api/v2/qq/people",get(crate::qq::api::people))
        .route("/api/v2/qq/decision/probe",post(crate::qq::decision::probe))
        .route("/api/v2/qq/groups",get(crate::qq::sessions::list).post(crate::qq::sessions::create))
        .route("/api/v2/qq/groups/{group}",axum::routing::put(crate::qq::sessions::update).delete(crate::qq::sessions::remove))
        .route("/api/v2/qq/groups/{group}/conversation",get(crate::qq::sessions::conversation))
        .route("/api/v2/qq/person",get(crate::qq::api::person))
        .route("/api/v2/qq/memories",post(crate::qq::api::remember))
        .route("/api/v2/qq/memories/{id}",axum::routing::delete(crate::qq::api::forget))
        .route("/api/v2/qq/reindex",post(crate::qq::api::reindex))
        .route("/api/v2/qq/stickers",get(crate::qq::stickers::list))
        .route("/api/v2/qq/stickers/settings",axum::routing::put(crate::qq::stickers::save_settings))
        .route("/api/v2/qq/stickers/retry",post(crate::qq::stickers::retry))
        .route("/api/v2/qq/stickers/{id}",axum::routing::patch(crate::qq::stickers::update).delete(crate::qq::stickers::delete))
        .route("/api/v2/qq/stickers/{id}/image",get(crate::qq::stickers::image))
        .route("/api/v2/ws",get(websocket))
        .route("/p/{id}",get(public_page))
        .fallback(||async{(StatusCode::NOT_FOUND,Json(json!({"ok":false,"code":"not_found","error":"接口不存在"})))})
        .layer(DefaultBodyLimit::max(1_200_000))
        .layer(SetResponseHeaderLayer::if_not_present(header::X_CONTENT_TYPE_OPTIONS,HeaderValue::from_static("nosniff")))
        .layer(SetResponseHeaderLayer::if_not_present(header::CACHE_CONTROL,HeaderValue::from_static("no-store")))
        .layer(SetResponseHeaderLayer::if_not_present(header::CONTENT_SECURITY_POLICY,HeaderValue::from_static("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")))
        .layer(TraceLayer::new_for_http()).with_state(app)
}
fn ok(value: impl serde::Serialize) -> Json<Value> {
    Json(json!({"ok":true,"data":value}))
}
async fn run_skill(
    State(app): State<App>,
    user: User,
    Path(id): Path<String>,
) -> Result<Json<Value>> {
    let skill = records::get(&app, &user, "skills", &id).await?;
    if skill.data["enabled"] != true {
        return Err(invalid("技能尚未启用"));
    }
    Ok(ok(crate::jobs::enqueue(
        &app,
        &user,
        crate::jobs::Input {
            action: "skill".into(),
            params: json!({"id":id}),
            run_at: None,
            request_id: None,
        },
    )
    .await?))
}
async fn ready(State(app): State<App>) -> Result<Json<Value>> {
    sqlx::query("SELECT 1").execute(&app.pool).await?;
    Ok(ok(json!({"database":true})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct MeInput {
    timezone: String,
}
async fn update_me(
    State(app): State<App>,
    user: User,
    Json(input): Json<MeInput>,
) -> Result<Json<Value>> {
    input
        .timezone
        .parse::<chrono_tz::Tz>()
        .map_err(|_| invalid("时区无效"))?;
    sqlx::query("UPDATE users SET timezone=? WHERE id=?")
        .bind(input.timezone)
        .bind(&user.id)
        .execute(&app.pool)
        .await?;
    Ok(ok(auth::user(&app, &user.id).await?))
}
async fn users(State(app): State<App>, user: User) -> Result<Json<Value>> {
    if !user.is_admin {
        return Err(Error::Forbidden);
    }
    Ok(ok(sqlx::query_as::<_, User>(
        "SELECT id,username,timezone,is_admin FROM users ORDER BY created_at",
    )
    .fetch_all(&app.pool)
    .await?))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct NewUser {
    username: String,
    password: String,
    #[serde(default)]
    is_admin: bool,
}
async fn create_user(
    State(app): State<App>,
    user: User,
    Json(input): Json<NewUser>,
) -> Result<(StatusCode, Json<Value>)> {
    if !user.is_admin {
        return Err(Error::Forbidden);
    }
    Ok((
        StatusCode::CREATED,
        ok(auth::create_user(&app, &input.username, input.password, input.is_admin).await?),
    ))
}
async fn tokens(State(app): State<App>, user: User) -> Result<Json<Value>> {
    let rows: Vec<(String, String, i64)> = sqlx::query_as(
        "SELECT hash,name,expires_at FROM tokens WHERE user_id=? ORDER BY created_at DESC",
    )
    .bind(user.id)
    .fetch_all(&app.pool)
    .await?;
    Ok(ok(rows
        .into_iter()
        .map(|(id, name, expires_at)| json!({"id":id,"name":name,"expires_at":expires_at}))
        .collect::<Vec<_>>()))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct NewToken {
    name: String,
    days: i64,
}
async fn create_token(
    State(app): State<App>,
    user: User,
    Json(input): Json<NewToken>,
) -> Result<Json<Value>> {
    if !(1..=365).contains(&input.days) || input.name.is_empty() || input.name.len() > 128 {
        return Err(invalid("Token 名称或有效期无效"));
    }
    Ok(ok(
        json!({"token":auth::issue_token(&app,&user,&input.name,input.days*86400).await?}),
    ))
}
async fn revoke_token(
    State(app): State<App>,
    user: User,
    Path(hash): Path<String>,
) -> Result<Json<Value>> {
    sqlx::query("DELETE FROM tokens WHERE hash=? AND user_id=?")
        .bind(hash)
        .bind(user.id)
        .execute(&app.pool)
        .await?;
    Ok(ok(json!({"revoked":true})))
}
async fn dashboard(State(app): State<App>, user: User) -> Result<Json<Value>> {
    let counts: Vec<(String, i64)> = sqlx::query_as(
        "SELECT kind,COUNT(*) FROM records WHERE user_id=? AND deleted_at IS NULL GROUP BY kind",
    )
    .bind(&user.id)
    .fetch_all(&app.pool)
    .await?;
    let pending:i64=sqlx::query_scalar("SELECT COUNT(*) FROM records WHERE user_id=? AND kind='tasks' AND status='open' AND deleted_at IS NULL").bind(&user.id).fetch_one(&app.pool).await?;
    let unread: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL",
    )
    .bind(&user.id)
    .fetch_one(&app.pool)
    .await?;
    let memories = crate::memory::list(&app, &user).await?.len();
    Ok(ok(
        json!({"counts":counts.into_iter().collect::<std::collections::BTreeMap<_,_>>(),"pending_tasks":pending,"unread_notifications":unread,"memories":memories}),
    ))
}
async fn list_records(
    State(app): State<App>,
    user: User,
    Path(kind): Path<String>,
    Query(filter): Query<records::List>,
) -> Result<Json<Value>> {
    Ok(ok(records::list(&app, &user, &kind, &filter).await?))
}
async fn create_record(
    State(app): State<App>,
    user: User,
    Path(kind): Path<String>,
    Json(input): Json<records::Input>,
) -> Result<(StatusCode, Json<Value>)> {
    Ok((
        StatusCode::CREATED,
        ok(records::create(&app, &user, &kind, input).await?),
    ))
}
async fn get_record(
    State(app): State<App>,
    user: User,
    Path((kind, id)): Path<(String, String)>,
) -> Result<Json<Value>> {
    Ok(ok(records::get(&app, &user, &kind, &id).await?))
}
async fn update_record(
    State(app): State<App>,
    user: User,
    Path((kind, id)): Path<(String, String)>,
    Json(input): Json<records::Input>,
) -> Result<Json<Value>> {
    Ok(ok(records::update(&app, &user, &kind, &id, input).await?))
}
async fn delete_record(
    State(app): State<App>,
    user: User,
    Path((kind, id)): Path<(String, String)>,
) -> Result<Json<Value>> {
    records::delete(&app, &user, &kind, &id).await?;
    Ok(ok(json!({"deleted":true})))
}
#[derive(Deserialize)]
struct Search {
    q: String,
}
async fn search(
    State(app): State<App>,
    user: User,
    Query(input): Query<Search>,
) -> Result<Json<Value>> {
    Ok(ok(crate::search::search(&app, &user, &input.q).await?))
}
async fn memories(State(app): State<App>, user: User) -> Result<Json<Value>> {
    Ok(ok(crate::memory::list(&app, &user).await?))
}
async fn remember(
    State(app): State<App>,
    user: User,
    Json(input): Json<crate::memory::Input>,
) -> Result<Json<Value>> {
    crate::memory::remember(&app, &user, &input, "manual", &json!([])).await?;
    Ok(ok(json!({"remembered":true})))
}
async fn forget(State(app): State<App>, user: User, Path(id): Path<String>) -> Result<Json<Value>> {
    crate::memory::forget(&app, &user, &id).await?;
    Ok(ok(json!({"deleted":true})))
}
async fn profiles(State(app): State<App>, user: User) -> Result<Json<Value>> {
    Ok(ok(crate::settings::profiles(&app, &user, false).await?))
}
async fn save_profiles(
    State(app): State<App>,
    user: User,
    Json(input): Json<Vec<crate::settings::Profile>>,
) -> Result<Json<Value>> {
    crate::settings::save_profiles(&app, &user, input).await?;
    Ok(ok(json!({"saved":true})))
}
async fn integrations(State(app): State<App>, user: User) -> Result<Json<Value>> {
    let mut cfg = crate::integrations::config(&app, &user).await?;
    crate::integrations::redact(&mut cfg);
    Ok(ok(cfg))
}
async fn save_integrations(
    State(app): State<App>,
    user: User,
    Json(input): Json<Value>,
) -> Result<Json<Value>> {
    crate::integrations::save_config(&app, &user, input).await?;
    Ok(ok(json!({"saved":true})))
}
async fn conversations(
    State(app): State<App>,
    user: User,
    Query(filter): Query<records::List>,
) -> Result<Json<Value>> {
    let (limit, offset) = filter.bounds()?;
    Ok(ok(sqlx::query_as::<_, chat::Conversation>(
        "SELECT * FROM conversations WHERE user_id=? ORDER BY updated_at DESC,id LIMIT ? OFFSET ?",
    )
    .bind(user.id)
    .bind(limit)
    .bind(offset)
    .fetch_all(&app.pool)
    .await?))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct NewConversation {
    #[serde(default = "new_title")]
    title: String,
    profile_id: Option<String>,
}
fn new_title() -> String {
    "新对话".into()
}
async fn create_conversation(
    State(app): State<App>,
    user: User,
    Json(input): Json<NewConversation>,
) -> Result<(StatusCode, Json<Value>)> {
    Ok((
        StatusCode::CREATED,
        ok(chat::create(&app, &user, &input.title, input.profile_id).await?),
    ))
}
async fn get_conversation(
    State(app): State<App>,
    user: User,
    Path(cid): Path<String>,
) -> Result<Json<Value>> {
    Ok(ok(chat::conversation(&app, &user, &cid).await?))
}
async fn update_conversation(
    State(app): State<App>,
    user: User,
    Path(cid): Path<String>,
    Json(input): Json<NewConversation>,
) -> Result<Json<Value>> {
    let lease = chat::Lease::acquire(&app, &user, &cid).await?;
    if input.title.trim().is_empty() || input.title.chars().count() > 255 {
        return Err(invalid("标题无效"));
    }
    if let Some(pid) = &input.profile_id {
        crate::settings::active_profile(&app, &user, Some(pid)).await?;
    }
    sqlx::query(
        "UPDATE conversations SET title=?,profile_id=?,updated_at=? WHERE id=? AND lease_token=?",
    )
    .bind(input.title)
    .bind(input.profile_id)
    .bind(now())
    .bind(&cid)
    .bind(&lease.token)
    .execute(&app.pool)
    .await?;
    Ok(ok(chat::conversation(&app, &user, &cid).await?))
}
async fn delete_conversation(
    State(app): State<App>,
    user: User,
    Path(cid): Path<String>,
) -> Result<Json<Value>> {
    let lease = chat::Lease::acquire(&app, &user, &cid).await?;
    sqlx::query("DELETE FROM conversations WHERE id=? AND user_id=? AND lease_token=?")
        .bind(cid)
        .bind(user.id)
        .bind(&lease.token)
        .execute(&app.pool)
        .await?;
    Ok(ok(json!({"deleted":true})))
}
#[derive(Deserialize)]
struct MessageQuery {
    #[serde(default)]
    after: i64,
    #[serde(default = "msg_limit")]
    limit: i64,
}
fn msg_limit() -> i64 {
    100
}
async fn messages(
    State(app): State<App>,
    user: User,
    Path(cid): Path<String>,
    Query(input): Query<MessageQuery>,
) -> Result<Json<Value>> {
    Ok(ok(chat::messages(
        &app,
        &user,
        &cid,
        input.after,
        input.limit,
    )
    .await?))
}
async fn send(
    State(app): State<App>,
    user: User,
    Path(cid): Path<String>,
    Json(input): Json<chat::Send>,
) -> Result<Response> {
    let session = chat::prepare(&app, &user, &cid, input).await?;
    let (tx, mut rx) = tokio::sync::mpsc::channel(32);
    let cancel = tokio_util::sync::CancellationToken::new();
    let guard = chat::CancelOnDrop(cancel.clone());
    tokio::spawn(async move {
        if let Err(error) = chat::run(app, session, Some(tx), cancel).await {
            tracing::debug!(%error,"chat finished with error");
        }
    });
    let stream = async_stream::stream! {let _guard=guard;while let Some(event)=rx.recv().await{
        yield Ok::<_,Infallible>(Event::default().event(&event.kind).json_data(&event).unwrap());
    }};
    let mut response = Sse::new(stream)
        .keep_alive(KeepAlive::new().interval(Duration::from_secs(15)))
        .into_response();
    response
        .headers_mut()
        .insert("x-accel-buffering", HeaderValue::from_static("no"));
    Ok(response)
}
async fn compact(
    State(app): State<App>,
    user: User,
    Path(cid): Path<String>,
) -> Result<Json<Value>> {
    let lease = chat::Lease::acquire(&app, &user, &cid).await?;
    let conv = chat::conversation(&app, &user, &cid).await?;
    let profile = crate::settings::active_profile(&app, &user, conv.profile_id.as_deref()).await?;
    Ok(ok(
        json!({"changed":chat::compact(&app,&user,&cid,&profile,&lease,true,&app.shutdown).await?}),
    ))
}
async fn jobs(
    State(app): State<App>,
    user: User,
    Query(filter): Query<records::List>,
) -> Result<Json<Value>> {
    let (limit, offset) = filter.bounds()?;
    Ok(ok(sqlx::query_as::<_, crate::jobs::Job>(
        "SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
    )
    .bind(user.id)
    .bind(limit)
    .bind(offset)
    .fetch_all(&app.pool)
    .await?))
}
async fn job(State(app): State<App>, user: User, Path(id): Path<String>) -> Result<Json<Value>> {
    Ok(ok(crate::jobs::get(&app, &user, &id).await?))
}
async fn enqueue(
    State(app): State<App>,
    user: User,
    Json(input): Json<crate::jobs::Input>,
) -> Result<(StatusCode, Json<Value>)> {
    Ok((
        StatusCode::ACCEPTED,
        ok(crate::jobs::enqueue(&app, &user, input).await?),
    ))
}
async fn cancel_job(
    State(app): State<App>,
    user: User,
    Path(id): Path<String>,
) -> Result<Json<Value>> {
    crate::jobs::get(&app, &user, &id).await?;
    sqlx::query("UPDATE jobs SET cancel_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=? AND user_id=?")
        .bind(id).bind(user.id).execute(&app.pool).await?;
    Ok(ok(json!({"cancel_requested":true})))
}
async fn schedules(State(app): State<App>, user: User) -> Result<Json<Value>> {
    let rows:Vec<(String,String,String,String,String,String,bool,i64)>=sqlx::query_as("SELECT id,name,action,params,cron,timezone,enabled,next_run_at FROM schedules WHERE user_id=? ORDER BY created_at").bind(user.id).fetch_all(&app.pool).await?;
    Ok(ok(rows.into_iter().map(|(id,name,action,params,cron,timezone,enabled,next_run_at)|json!({"id":id,"name":name,"action":action,"params":serde_json::from_str::<Value>(&params).unwrap_or(Value::Null),"cron":cron,"timezone":timezone,"enabled":enabled,"next_run_at":next_run_at})).collect::<Vec<_>>()))
}
async fn create_schedule(
    State(app): State<App>,
    user: User,
    Json(input): Json<crate::jobs::ScheduleInput>,
) -> Result<Json<Value>> {
    Ok(ok(
        json!({"id":crate::jobs::add_schedule(&app,&user,input).await?}),
    ))
}
async fn delete_schedule(
    State(app): State<App>,
    user: User,
    Path(id): Path<String>,
) -> Result<Json<Value>> {
    sqlx::query("DELETE FROM schedules WHERE id=? AND user_id=?")
        .bind(id)
        .bind(user.id)
        .execute(&app.pool)
        .await?;
    Ok(ok(json!({"deleted":true})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Toggle {
    enabled: bool,
}
async fn toggle_schedule(
    State(app): State<App>,
    user: User,
    Path(id): Path<String>,
    Json(input): Json<Toggle>,
) -> Result<Json<Value>> {
    sqlx::query("UPDATE schedules SET enabled=? WHERE id=? AND user_id=?")
        .bind(input.enabled)
        .bind(id)
        .bind(user.id)
        .execute(&app.pool)
        .await?;
    Ok(ok(json!({"saved":true})))
}
async fn notifications(
    State(app): State<App>,
    user: User,
    Query(filter): Query<records::List>,
) -> Result<Json<Value>> {
    let (limit, offset) = filter.bounds()?;
    let rows:Vec<(String,String,String,Option<i64>,i64)>=sqlx::query_as("SELECT id,title,body,read_at,created_at FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?").bind(user.id).bind(limit).bind(offset).fetch_all(&app.pool).await?;
    Ok(ok(rows.into_iter().map(|(id,title,body,read_at,created_at)|json!({"id":id,"title":title,"body":body,"read_at":read_at,"created_at":created_at})).collect::<Vec<_>>()))
}
async fn read_notification(
    State(app): State<App>,
    user: User,
    Path(id): Path<String>,
) -> Result<Json<Value>> {
    sqlx::query("UPDATE notifications SET read_at=? WHERE id=? AND user_id=?")
        .bind(now())
        .bind(id)
        .bind(user.id)
        .execute(&app.pool)
        .await?;
    Ok(ok(json!({"read":true})))
}
async fn storage(State(app): State<App>, user: User) -> Result<Json<Value>> {
    Ok(ok(crate::media::list_storage(&app, &user).await?))
}
#[derive(Deserialize)]
struct FileQuery {
    #[serde(default)]
    path: String,
}
async fn files(
    State(app): State<App>,
    user: User,
    Path(id): Path<String>,
    Query(input): Query<FileQuery>,
) -> Result<Json<Value>> {
    Ok(ok(
        crate::media::list_files(&app, &user, &id, &input.path).await?
    ))
}
async fn asset(State(app): State<App>, user: User, Path(id): Path<String>) -> Result<Response> {
    let record = records::get(&app, &user, "images", &id).await?;
    let file = record.data["file"].as_str().ok_or(Error::NotFound)?;
    if file.contains(['/', '\\']) || file.starts_with('.') {
        return Err(Error::NotFound);
    }
    let bytes = tokio::fs::read(app.data_dir.join("assets").join(user.id).join(file))
        .await
        .map_err(|_| Error::NotFound)?;
    let mime = if file.ends_with(".png") {
        "image/png"
    } else if file.ends_with(".webp") {
        "image/webp"
    } else {
        "image/jpeg"
    };
    Ok(([(header::CONTENT_TYPE, mime)], bytes).into_response())
}
async fn public_page(State(app): State<App>, Path(id): Path<String>) -> Result<Response> {
    let html:Option<String>=sqlx::query_scalar("SELECT content FROM records WHERE id=? AND kind='pages' AND deleted_at IS NULL AND json_extract(data,'$.public')=1")
        .bind(id).fetch_optional(&app.pool).await?;
    let mut response = Html(html.ok_or(Error::NotFound)?).into_response();
    response.headers_mut().insert(header::CONTENT_SECURITY_POLICY,HeaderValue::from_static("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: https:; form-action 'none'; base-uri 'none'; frame-ancestors 'none'"));
    Ok(response)
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Bridge {
    channel: String,
    external_id: String,
    message: String,
    request_id: String,
}
async fn bridge(
    State(app): State<App>,
    user: User,
    Json(input): Json<Bridge>,
) -> Result<Json<Value>> {
    if !["feishu", "onebot", "astrbot", "generic"].contains(&input.channel.as_str())
        || input.external_id.is_empty()
        || input.external_id.len() > 128
    {
        return Err(invalid("渠道或外部会话 ID 无效"));
    }
    let existing:Option<String>=sqlx::query_scalar("SELECT conversation_id FROM channel_bindings WHERE user_id=? AND channel=? AND external_id=?")
        .bind(&user.id).bind(&input.channel).bind(&input.external_id).fetch_optional(&app.pool).await?;
    let cid = if let Some(cid) = existing {
        cid
    } else {
        let conv = chat::create(&app, &user, &format!("{} 对话", input.channel), None).await?;
        sqlx::query("INSERT INTO channel_bindings(user_id,channel,external_id,conversation_id) VALUES(?,?,?,?) ON CONFLICT DO NOTHING")
            .bind(&user.id).bind(&input.channel).bind(&input.external_id).bind(&conv.id).execute(&app.pool).await?;
        sqlx::query_scalar::<_,String>("SELECT conversation_id FROM channel_bindings WHERE user_id=? AND channel=? AND external_id=?")
            .bind(&user.id).bind(&input.channel).bind(&input.external_id).fetch_one(&app.pool).await?
    };
    let key = crate::db::digest(&format!(
        "{}:{}:{}",
        input.channel, input.external_id, input.request_id
    ));
    let session = chat::prepare(
        &app,
        &user,
        &cid,
        chat::Send {
            message: input.message,
            request_id: key,
        },
    )
    .await?;
    let reply = chat::run(app.clone(), session, None, app.shutdown.child_token()).await?;
    Ok(ok(json!({"conversation_id":cid,"reply":reply})))
}
async fn websocket(State(app): State<App>, ws: WebSocketUpgrade) -> Response {
    ws.max_message_size(550_000)
        .on_upgrade(move |socket| ws_session(app, socket))
}
async fn ws_session(app: App, mut socket: WebSocket) {
    let auth = tokio::time::timeout(Duration::from_secs(10), socket.recv()).await;
    let token = if let Ok(Some(Ok(WsMessage::Text(text)))) = auth {
        serde_json::from_str::<Value>(&text)
            .ok()
            .and_then(|v| v["token"].as_str().map(String::from))
            .unwrap_or_default()
    } else {
        String::new()
    };
    let Ok(user) = auth::resolve(&app, &token).await else {
        let _ = socket.send(WsMessage::Close(None)).await;
        return;
    };
    let _ = socket
        .send(WsMessage::Text(
            json!({"type":"authenticated","data":user})
                .to_string()
                .into(),
        ))
        .await;
    while let Some(Ok(WsMessage::Text(text))) = socket.recv().await {
        if auth::resolve(&app, &token).await.is_err() {
            break;
        }
        let Ok(input) = serde_json::from_str::<Value>(&text) else {
            break;
        };
        let cid = input["conversation_id"].as_str().unwrap_or("");
        let request = chat::Send {
            message: input["message"].as_str().unwrap_or("").into(),
            request_id: input["request_id"].as_str().unwrap_or("").into(),
        };
        let session = match chat::prepare(&app, &user, cid, request).await {
            Ok(s) => s,
            Err(e) => {
                let _ = socket
                    .send(WsMessage::Text(
                        json!({"type":"error","data":e.to_string()})
                            .to_string()
                            .into(),
                    ))
                    .await;
                continue;
            }
        };
        let (tx, mut rx) = tokio::sync::mpsc::channel(32);
        let cancel = tokio_util::sync::CancellationToken::new();
        let guard = chat::CancelOnDrop(cancel.clone());
        let cloned = app.clone();
        tokio::spawn(async move {
            let _ = chat::run(cloned, session, Some(tx), cancel).await;
        });
        loop {
            tokio::select! {
                event=rx.recv()=>{let Some(event)=event else{break};if socket.send(WsMessage::Text(serde_json::to_string(&event).unwrap().into())).await.is_err(){return;}},
                incoming=socket.recv()=>{match incoming{Some(Ok(WsMessage::Text(text))) if text.as_str()=="cancel"=>{guard.0.cancel();},Some(Ok(WsMessage::Ping(_)))=>(),_=>return}}
            }
        }
    }
}
