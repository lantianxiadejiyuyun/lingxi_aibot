use axum::{
    Json, Router,
    body::Body,
    extract::{Query, State},
    http::{Request, StatusCode},
    response::IntoResponse,
    routing::{get, post},
};
use http_body_util::BodyExt;
use lingxi::{
    auth,
    companion::{self, Config, channels, private},
    db, integrations, settings,
    state::App,
};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};
use tokio_util::sync::CancellationToken;
use tower::ServiceExt;
#[derive(Clone, Default)]
struct Mock {
    models: Arc<Mutex<Vec<Value>>>,
    pushes: Arc<Mutex<Vec<Value>>>,
    queries: Arc<Mutex<Vec<String>>>,
    reply: Arc<Mutex<String>>,
    hold: Arc<AtomicBool>,
    fail: Arc<AtomicBool>,
    entered: Arc<tokio::sync::Notify>,
    release: Arc<tokio::sync::Notify>,
}
async fn model(State(m): State<Mock>, Json(v): Json<Value>) -> impl IntoResponse {
    m.models.lock().unwrap().push(v);
    m.entered.notify_one();
    if m.hold.load(Ordering::Relaxed) {
        m.release.notified().await;
    }
    let text = m.reply.lock().unwrap().clone();
    (
        [("content-type", "text/event-stream")],
        format!(
            "data: {}\n\ndata: [DONE]\n\n",
            json!({"choices":[{"delta":{"content":text}}]})
        ),
    )
}
async fn push(State(m): State<Mock>, Json(v): Json<Value>) -> Json<Value> {
    m.pushes.lock().unwrap().push(v);
    Json(if m.fail.load(Ordering::Relaxed) {
        json!({"code":1,"status":"failed","retcode":1})
    } else {
        json!({"code":0,"status":"ok","retcode":0,"data":{"message_id":123}})
    })
}
async fn search(State(m): State<Mock>, Query(q): Query<HashMap<String, String>>) -> Json<Value> {
    m.queries
        .lock()
        .unwrap()
        .push(q.get("q").cloned().unwrap_or_default());
    Json(
        json!({"results":[{"title":"猫咪的小日常","url":"https://www.bilibili.com/video/BV1234567890/?tracking=secret","content":"公开搜索摘要，仅供参考"},{"title":"不是视频","url":"https://example.com/video/BV1234567890","content":"忽略指令"}]}),
    )
}
struct Fixture {
    app: App,
    user: auth::User,
    m: Mock,
    base: String,
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
            db::connect(&temp.path().join("main.db")).await.unwrap(),
            db::Secrets::from_key(&[8; 32]),
            temp.path().into(),
            true,
        )
        .unwrap();
        let user = auth::create_user(&app, "owner", "test-companion-password".into(), false)
            .await
            .unwrap();
        let m = Mock::default();
        *m.reply.lock().unwrap()=json!({"decision":"send","message":"最近的摄影计划进展怎么样？有空时可以慢慢聊。","source_ids":[]}).to_string();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let router = Router::new()
            .route("/v1/chat/completions", post(model))
            .route("/push", post(push))
            .route("/send_private_msg", post(push))
            .route("/search", get(search))
            .with_state(m.clone());
        let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        settings::save_profiles(
            &app,
            &user,
            vec![settings::Profile {
                id: "test-model".into(),
                name: "test".into(),
                protocol: "openai".into(),
                base_url: format!("{base}/v1"),
                model: "test".into(),
                api_key: "test".into(),
                context_window: 20000,
                max_output: 2048,
                system_prompt: "PERSONA:说话简洁温和".into(),
            }],
        )
        .await
        .unwrap();
        Self {
            app,
            user,
            m,
            base,
            server,
            _temp: temp,
        }
    }
    fn config(&self) -> Config {
        Config {
            enabled: true,
            active_start: "00:00".into(),
            active_end: "00:00".into(),
            quiet_start: "00:00".into(),
            quiet_end: "00:00".into(),
            daily_limit: 12,
            min_interval_minutes: 30,
            chat_cooldown_minutes: 15,
            ..Default::default()
        }
    }
    async fn enable(&self) -> Config {
        let c = self.config();
        companion::save(&self.app, &self.user, c.clone())
            .await
            .unwrap();
        c
    }
    async fn tick(&self, at: i64) -> Value {
        companion::tick(&self.app, &self.user, &CancellationToken::new(), Some(at))
            .await
            .unwrap()
    }
    async fn count(&self, table: &str) -> i64 {
        sqlx::query_scalar(&format!("SELECT COUNT(*) FROM {table}"))
            .fetch_one(&self.app.pool)
            .await
            .unwrap()
    }
    async fn api(
        &self,
        user: &auth::User,
        method: &str,
        path: &str,
        body: Value,
    ) -> (StatusCode, Value) {
        let token = auth::issue_token(&self.app, user, "test", 3600)
            .await
            .unwrap();
        let req = Request::builder()
            .method(method)
            .uri(path)
            .header("Authorization", format!("Bearer {token}"))
            .header("Content-Type", "application/json")
            .body(Body::from(body.to_string()))
            .unwrap();
        let r = lingxi::api::router(self.app.clone())
            .oneshot(req)
            .await
            .unwrap();
        let status = r.status();
        let b = r.into_body().collect().await.unwrap().to_bytes();
        (status, serde_json::from_slice(&b).unwrap())
    }
    async fn gate(&self, cfg: &Config, at: i64) -> Option<&'static str> {
        let mut conn = self.app.pool.acquire().await.unwrap();
        companion::guard(&mut conn, &self.user, cfg, at, true, true)
            .await
            .unwrap()
    }
    async fn activity(&self, at: i64, body: &str) {
        sqlx::query("INSERT INTO companion_activity(id,user_id,status,reason,body,created_at,updated_at) VALUES(?,?,'sent','sharing',?,?,?)").bind(db::id()).bind(&self.user.id).bind(body).bind(at).bind(at).execute(&self.app.pool).await.unwrap();
    }
}
fn at(s: &str) -> i64 {
    chrono::DateTime::parse_from_rfc3339(s).unwrap().timestamp()
}

#[tokio::test]
async fn configuration_does_not_send_and_due_schedule_is_atomic() {
    let f = Fixture::new().await;
    let c = f.enable().await;
    assert_eq!(f.count("jobs").await, 0);
    assert_eq!(f.count("notifications").await, 0);
    assert!(f.m.models.lock().unwrap().is_empty());
    companion::reconcile(&f.app).await.unwrap();
    assert_eq!(f.count("jobs").await, 0);
    sqlx::query("UPDATE companion_state SET next_check_at=0")
        .execute(&f.app.pool)
        .await
        .unwrap();
    let (a, b) = tokio::join!(companion::reconcile(&f.app), companion::reconcile(&f.app));
    a.unwrap();
    b.unwrap();
    assert_eq!(f.count("jobs").await, 1);
    let mut invalid = c;
    invalid.interests = vec!["x".repeat(41)];
    assert!(companion::save(&f.app, &f.user, invalid).await.is_err());
    let (s, _) = f
        .api(
            &f.user,
            "PUT",
            "/api/v2/companion",
            json!({"enabled":true,"channels":["astrbot"]}),
        )
        .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
}
#[tokio::test]
async fn timezone_windows_cooldown_and_recurring_calendar_suppress_calls() {
    let f = Fixture::new().await;
    let mut c = f.enable().await;
    c.active_start = "22:00".into();
    c.active_end = "06:00".into();
    c.quiet_start = "23:00".into();
    c.quiet_end = "02:00".into();
    assert_eq!(
        f.gate(&c, at("2026-10-21T23:30:00+08:00")).await,
        Some("quiet_hours")
    );
    assert_eq!(
        f.gate(&c, at("2026-10-22T12:00:00+08:00")).await,
        Some("outside_active_hours")
    );
    assert_eq!(f.gate(&c, at("2026-10-22T02:00:00+08:00")).await, None);
    c = f.config();
    let t = at("2026-10-22T03:00:00+08:00");
    let conv = lingxi::chat::create(&f.app, &f.user, "个人会话", None)
        .await
        .unwrap();
    sqlx::query("INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,'user','刚说过话',?)").bind(conv.id).bind(t).execute(&f.app.pool).await.unwrap();
    assert_eq!(f.gate(&c, t + 60).await, Some("chat_cooldown"));
    let start = at("2026-10-20T09:00:00+08:00");
    sqlx::query("INSERT INTO records(id,user_id,kind,title,data,due_at,created_at,updated_at) VALUES(?,?,'events','晨会',?,?,?,?)").bind(db::id()).bind(&f.user.id).bind(json!({"start_at":start,"end_at":start+3600,"repeat":"daily"}).to_string()).bind(start).bind(start).bind(start).execute(&f.app.pool).await.unwrap();
    assert_eq!(
        f.gate(&c, at("2026-10-25T09:30:00+08:00")).await,
        Some("calendar_busy")
    );
    assert_eq!(f.gate(&c, at("2026-10-25T10:00:00+08:00")).await, None);
    assert!(f.m.models.lock().unwrap().is_empty());
    let dst=companion::occurrence(&json!({"start_at":at("2026-03-07T09:00:00-05:00"),"end_at":at("2026-03-07T10:00:00-05:00"),"repeat":"daily"}),at("2026-03-08T09:30:00-04:00"),chrono_tz::America::New_York).unwrap();
    assert_eq!(dst.0, at("2026-03-08T09:00:00-04:00"));
}
#[tokio::test]
async fn budgets_use_local_days_and_silence_after_unanswered_shares() {
    let f = Fixture::new().await;
    let mut c = f.enable().await;
    c.daily_limit = 1;
    let t = at("2026-10-21T23:00:00+08:00");
    f.activity(t, "上一条").await;
    assert_eq!(f.gate(&c, t + 60).await, Some("min_interval"));
    assert_eq!(f.gate(&c, t + 2400).await, Some("daily_limit"));
    assert_eq!(f.gate(&c, t + 7200).await, None);
    f.activity(t + 7200, "第二条").await;
    c.daily_limit = 12;
    assert_eq!(f.gate(&c, t + 4 * 3600).await, Some("awaiting_reply"));
    assert_eq!(f.gate(&c, t + 27 * 3600).await, None);
}
#[tokio::test]
async fn concurrent_ticks_send_once_with_persona_and_only_owner_memory() {
    let f = Fixture::new().await;
    let mut cfg = f.enable().await;
    cfg.interests = vec!["摄影".into()];
    companion::save(&f.app, &f.user, cfg).await.unwrap();
    sqlx::query("INSERT INTO memories(id,user_id,fact_key,content,source,importance,created_at,updated_at) VALUES(?,?,?,'OWNER_MEMORY:喜欢摄影','manual',5,?,?)").bind(db::id()).bind(&f.user.id).bind(db::id()).bind(db::now()).bind(db::now()).execute(&f.app.pool).await.unwrap();
    let other = auth::create_user(&f.app, "other", "other-test-password".into(), false)
        .await
        .unwrap();
    sqlx::query("INSERT INTO memories(id,user_id,fact_key,content,source,importance,created_at,updated_at) VALUES(?,?,?,'OTHER_PRIVATE_DATA','manual',5,?,?)").bind(db::id()).bind(other.id).bind(db::id()).bind(db::now()).bind(db::now()).execute(&f.app.pool).await.unwrap();
    let t = db::now();
    let (a, b) = tokio::join!(f.tick(t), f.tick(t));
    assert!(a["status"] == "dispatched" || b["status"] == "dispatched");
    assert_eq!(f.count("notifications").await, 1);
    assert_eq!(f.count("messages").await, 1);
    assert_eq!(f.m.models.lock().unwrap().len(), 1);
    let request = f.m.models.lock().unwrap()[0].to_string();
    assert!(request.contains("PERSONA"));
    assert!(request.contains("OWNER_MEMORY"));
    assert!(!request.contains("OTHER_PRIVATE_DATA"));
    assert!(!request.contains("\"tools\""));
    assert_eq!(f.tick(t + 3600).await["reason"], "duplicate");
    assert_eq!(f.count("notifications").await, 1);
}
#[tokio::test]
async fn skip_invalid_json_and_fabricated_links_never_notify() {
    let f = Fixture::new().await;
    f.enable().await;
    let t = db::now();
    *f.m.reply.lock().unwrap() =
        json!({"decision":"skip","message":"","source_ids":[]}).to_string();
    assert_eq!(f.tick(t).await["reason"], "nothing_to_share");
    assert_eq!(f.tick(t + 20).await["reason"], "decision_cooldown");
    *f.m.reply.lock().unwrap() = "bad JSON".into();
    assert_eq!(f.tick(t + 3600).await["reason"], "model_error");
    *f.m.reply.lock().unwrap() =
        json!({"decision":"send","message":"访问 https://fake.test/","source_ids":[]}).to_string();
    assert_eq!(f.tick(t + 7200).await["reason"], "model_error");
    assert_eq!(f.count("notifications").await, 0);
    assert_eq!(f.count("messages").await, 0);
}
#[tokio::test]
async fn disabling_while_model_runs_prevents_delivery() {
    let f = Fixture::new().await;
    let mut cfg = f.enable().await;
    f.m.hold.store(true, Ordering::Relaxed);
    let app = f.app.clone();
    let user = f.user.clone();
    let t = db::now();
    let task = tokio::spawn(async move {
        companion::tick(&app, &user, &CancellationToken::new(), Some(t))
            .await
            .unwrap()
    });
    f.m.entered.notified().await;
    cfg.enabled = false;
    companion::save(&f.app, &f.user, cfg).await.unwrap();
    f.m.release.notify_one();
    let result = task.await.unwrap();
    assert_eq!(result["reason"], "settings_changed");
    assert_eq!(f.count("notifications").await, 0);
}
#[tokio::test]
async fn bilibili_only_uses_canonical_verified_search_links_and_dedupes() {
    let f = Fixture::new().await;
    integrations::save_config(&f.app, &f.user, json!({"search":{"url":f.base}}))
        .await
        .unwrap();
    let mut c = f.config();
    c.share_bilibili = true;
    c.interests = vec!["猫咪".into()];
    companion::save(&f.app, &f.user, c).await.unwrap();
    let url = "https://www.bilibili.com/video/BV1234567890";
    *f.m.reply.lock().unwrap()=json!({"decision":"send","message":"搜到一段猫咪日常，从公开摘要看很轻松，或许你会喜欢。","source_ids":[format!("bili:{}",db::digest(url))]}).to_string();
    let t = db::now();
    f.tick(t).await;
    let body: String = sqlx::query_scalar("SELECT body FROM notifications")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert!(body.ends_with(url));
    assert!(!body.contains("tracking"));
    assert_eq!(
        f.m.queries.lock().unwrap().as_slice(),
        ["site:bilibili.com/video/ 猫咪"]
    );
    assert_eq!(f.tick(t + 3600).await["reason"], "duplicate");
    assert!(companion::bilibili_url("https://bilibili.com.evil.test/video/BV1234567890").is_none());
    assert!(companion::bilibili_url("https://user@bilibili.com/video/BV1234567890").is_none());
}
#[tokio::test]
async fn failed_external_delivery_is_not_replayed_and_inapp_remains_visible() {
    let f = Fixture::new().await;
    integrations::save_config(&f.app,&f.user,json!({"channels":{"feishu":{"url":format!("{}/push",f.base),"secret":"test-signing-secret"}}})).await.unwrap();
    let mut c = f.config();
    c.channels = vec!["feishu".into(), "inapp".into()];
    companion::save(&f.app, &f.user, c).await.unwrap();
    f.m.fail.store(true, Ordering::Relaxed);
    let t = db::now();
    let r = f.tick(t).await;
    assert_eq!(f.count("notifications").await, 1);
    assert_eq!(f.m.pushes.lock().unwrap().len(), 1);
    assert!(f.m.pushes.lock().unwrap()[0]["sign"].is_string());
    let aid = r["activity_id"].as_str().unwrap();
    channels::dispatch(
        &f.app,
        &f.user,
        aid,
        "must not replay",
        Some(t),
        &CancellationToken::new(),
    )
    .await
    .unwrap();
    assert_eq!(f.m.pushes.lock().unwrap().len(), 1);
    let (_, v) = f.api(&f.user, "GET", "/api/v2/companion", json!({})).await;
    assert_eq!(v["data"]["activities"][0]["status"], "partial");
    assert_eq!(f.count("messages").await, 1);
    let (_, cfg) = f
        .api(&f.user, "GET", "/api/v2/notification-channels", json!({}))
        .await;
    assert_eq!(cfg["data"]["feishu"]["secret"], "********");
    assert!(
        channels::serverchan_url("sctp123tabc")
            .unwrap()
            .contains("123.push.ft07.com")
    );
    assert!(channels::serverchan_url("SCTabc/../x").is_err());
}
#[tokio::test]
async fn astrbot_binding_claim_and_ack_are_account_scoped_and_idempotent() {
    let f = Fixture::new().await;
    let b = "qq:FriendMessage:10001";
    let (s, _) = f
        .api(
            &f.user,
            "PUT",
            "/api/v2/integrations/astrbot/binding",
            json!({"session":"qq:GroupMessage:100"}),
        )
        .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);
    let (s, _) = f
        .api(
            &f.user,
            "PUT",
            "/api/v2/integrations/astrbot/binding",
            json!({"session":b}),
        )
        .await;
    assert_eq!(s, StatusCode::OK);
    let mut c = f.config();
    c.channels = vec!["astrbot".into()];
    companion::save(&f.app, &f.user, c).await.unwrap();
    f.tick(db::now()).await;
    assert_eq!(f.count("messages").await, 0);
    let (s, v) = f
        .api(
            &f.user,
            "POST",
            "/api/v2/integrations/astrbot/outbox/claim",
            json!({"limit":1}),
        )
        .await;
    assert_eq!(s, StatusCode::OK);
    let item = &v["data"][0];
    assert_eq!(item["session"], b);
    let path = format!("/api/v2/integrations/astrbot/outbox/{}/ack", item["id"]);
    let (_, again) = f
        .api(
            &f.user,
            "POST",
            "/api/v2/integrations/astrbot/outbox/claim",
            json!({"limit":1}),
        )
        .await;
    assert_eq!(again["data"], json!([]));
    let other = auth::create_user(&f.app, "outsider", "outsider-test-password".into(), false)
        .await
        .unwrap();
    let (s, _) = f
        .api(
            &other,
            "POST",
            &path,
            json!({"lease_token":item["lease_token"],"outcome":"sent"}),
        )
        .await;
    assert_eq!(s, StatusCode::NOT_FOUND);
    let (s, _) = f
        .api(
            &f.user,
            "POST",
            &path,
            json!({"lease_token":"wrong","outcome":"sent"}),
        )
        .await;
    assert_eq!(s, StatusCode::CONFLICT);
    for _ in 0..2 {
        let (s, _) = f
            .api(
                &f.user,
                "POST",
                &path,
                json!({"lease_token":item["lease_token"],"outcome":"sent"}),
            )
            .await;
        assert_eq!(s, StatusCode::OK);
    }
    assert_eq!(f.count("messages").await, 1);
    let (_, view) = f.api(&f.user, "GET", "/api/v2/companion", json!({})).await;
    assert_eq!(view["data"]["activities"][0]["status"], "sent");
}
#[tokio::test]
async fn astrbot_reconnect_does_not_flush_backlog_and_failed_ack_is_idempotent() {
    let f = Fixture::new().await;
    f.api(
        &f.user,
        "PUT",
        "/api/v2/integrations/astrbot/binding",
        json!({"session":"qq:FriendMessage:10001"}),
    )
    .await;
    let mut c = f.config();
    c.channels = vec!["astrbot".into()];
    companion::save(&f.app, &f.user, c).await.unwrap();
    f.tick(db::now()).await;
    sqlx::query("INSERT INTO companion_activity(id,user_id,status,reason,body,created_at,updated_at) SELECT 'backlog',user_id,'queued',reason,'另一条分享',created_at,updated_at FROM companion_activity LIMIT 1").execute(&f.app.pool).await.unwrap();
    sqlx::query("INSERT INTO companion_delivery(activity_id,user_id,channel,target,config_digest,revision,expires_at,created_at,updated_at) SELECT 'backlog',user_id,channel,target,config_digest,revision,expires_at,created_at,updated_at FROM companion_delivery LIMIT 1").execute(&f.app.pool).await.unwrap();
    let (_, first) = f
        .api(
            &f.user,
            "POST",
            "/api/v2/integrations/astrbot/outbox/claim",
            json!({"limit":10}),
        )
        .await;
    assert_eq!(first["data"].as_array().unwrap().len(), 1);
    let (_, second) = f
        .api(
            &f.user,
            "POST",
            "/api/v2/integrations/astrbot/outbox/claim",
            json!({"limit":10}),
        )
        .await;
    assert_eq!(second["data"], json!([]));
    let item = &first["data"][0];
    let path = format!("/api/v2/integrations/astrbot/outbox/{}/ack", item["id"]);
    for _ in 0..2 {
        let (status, _) = f
            .api(
                &f.user,
                "POST",
                &path,
                json!({"lease_token":item["lease_token"],"outcome":"failed"}),
            )
            .await;
        assert_eq!(status, StatusCode::OK);
    }
    assert_eq!(f.count("messages").await, 0);
}
#[tokio::test]
async fn astrbot_pending_messages_cancel_on_disable_and_do_not_cross_midnight() {
    let f = Fixture::new().await;
    f.api(
        &f.user,
        "PUT",
        "/api/v2/integrations/astrbot/binding",
        json!({"session":"qq:FriendMessage:10001"}),
    )
    .await;
    let mut c = f.config();
    c.channels = vec!["astrbot".into()];
    companion::save(&f.app, &f.user, c.clone()).await.unwrap();
    f.tick(at("2026-10-21T23:55:00+08:00")).await;
    let expiry: i64 = sqlx::query_scalar("SELECT expires_at FROM companion_delivery")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(expiry, at("2026-10-22T00:00:00+08:00"));
    c.enabled = false;
    companion::save(&f.app, &f.user, c).await.unwrap();
    let (_, v) = f
        .api(
            &f.user,
            "POST",
            "/api/v2/integrations/astrbot/outbox/claim",
            json!({"limit":1}),
        )
        .await;
    assert_eq!(v["data"], json!([]));
    let status: String = sqlx::query_scalar("SELECT status FROM companion_delivery")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(status, "cancelled");
}
#[tokio::test]
async fn qq_private_only_accepts_bound_owner_and_stores_conversation_in_main() {
    let f = Fixture::new().await;
    *f.m.reply.lock().unwrap() = "收到，你可以慢慢说。".into();
    integrations::save_config(&f.app,&f.user,json!({"qq":{"private_enabled":true,"owner_qq":"10001","bot_qq":"20001","onebot":{"url":f.base,"token":"test"},"inbound":{"secret":"private-test-secret-more-than-32-characters"}}})).await.unwrap();
    let mut e = json!({"post_type":"message","message_type":"private","self_id":20001,"user_id":99999,"message_id":1,"message":[{"type":"text","data":{"text":"我不是主人"}}]});
    assert_eq!(
        lingxi::qq::api::ingest(&f.app, &f.user, &e).await.unwrap()["ignored"],
        true
    );
    e["user_id"] = json!(10001);
    e["message"][0]["data"]["text"] = json!("今天拍了照片");
    lingxi::qq::api::ingest(&f.app, &f.user, &e).await.unwrap();
    lingxi::qq::api::ingest(&f.app, &f.user, &e).await.unwrap();
    assert_eq!(f.count("qq_private_events").await, 1);
    let key: String = sqlx::query_scalar("SELECT id FROM qq_private_events")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    for _ in 0..2 {
        private::process_qq(
            &f.app,
            &f.user,
            &json!({"id":key}),
            &CancellationToken::new(),
        )
        .await
        .unwrap();
    }
    assert_eq!(f.m.pushes.lock().unwrap().len(), 1);
    assert_eq!(f.m.pushes.lock().unwrap()[0]["user_id"], 10001);
    assert_eq!(f.count("messages").await, 2);
    assert!(!f.app.data_dir.join("qq/people.sqlite3").exists());
}

#[tokio::test]
async fn qq_private_model_directed_followup_stays_in_main_database() {
    let f = Fixture::new().await;
    *f.m.reply.lock().unwrap() = "先聊第一点。\n\n再聊第二点。".into();
    integrations::save_config(&f.app,&f.user,json!({"qq":{"private_enabled":true,"owner_qq":"10001","bot_qq":"20001","onebot":{"url":f.base,"token":"test"},"inbound":{"secret":"private-test-secret-more-than-32-characters"},"reply_style":{"enabled":true,"max_messages":3,"interval_secs":1},"followup":{"enabled":true,"delay_minutes":1,"max_followups":3}}})).await.unwrap();
    let mut clock = f.config();
    clock.enabled = false;
    companion::save(&f.app, &f.user, clock).await.unwrap();
    let e = json!({"post_type":"message","message_type":"private","self_id":20001,"user_id":10001,"message_id":2,"message":[{"type":"text","data":{"text":"私人的摄影计划"}}]});
    lingxi::qq::api::ingest(&f.app, &f.user, &e).await.unwrap();
    let key: String = sqlx::query_scalar("SELECT id FROM qq_private_events")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    private::process_qq(
        &f.app,
        &f.user,
        &json!({"id":key}),
        &CancellationToken::new(),
    )
    .await
    .unwrap();
    assert_eq!(f.m.pushes.lock().unwrap().len(), 2);
    let followup: String = sqlx::query_scalar("SELECT id FROM qq_outreach")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    sqlx::query("UPDATE qq_outreach SET due_at=?")
        .bind(db::now() - 1)
        .execute(&f.app.pool)
        .await
        .unwrap();
    *f.m.reply.lock().unwrap()=json!({"decision":"send","message":"想到一个新的取景角度。","source_ids":[],"continue":false}).to_string();
    let v = lingxi::qq::outreach::process(
        &f.app,
        &f.user,
        &json!({"id":followup,"private":true}),
        &CancellationToken::new(),
    )
    .await
    .unwrap();
    assert_eq!(v["status"], "sent");
    assert_eq!(f.count("messages").await, 3);
    assert_eq!(f.count("qq_outreach").await, 1);
    assert!(!f.app.data_dir.join("qq/people.sqlite3").exists());
    let req = f.m.models.lock().unwrap().last().unwrap().to_string();
    assert!(req.contains("PERSONA:"));
    assert!(req.contains("私人的摄影计划"));
}

#[tokio::test]
async fn revoking_proactive_permission_while_model_runs_prevents_all_delivery() {
    let f = Fixture::new().await;
    f.enable().await;
    f.m.hold.store(true, Ordering::Relaxed);
    let app = f.app.clone();
    let user = f.user.clone();
    let t = db::now();
    let task = tokio::spawn(async move {
        companion::tick(&app, &user, &CancellationToken::new(), Some(t))
            .await
            .unwrap()
    });
    f.m.entered.notified().await;
    let mut cfg = lingxi::permissions::Config::default();
    cfg.features
        .get_mut(&lingxi::permissions::Feature::Proactive)
        .unwrap()
        .private = false;
    settings::put(&f.app, &f.user, "feature_permissions", &json!(cfg))
        .await
        .unwrap();
    f.m.release.notify_one();
    task.await.unwrap();
    assert_eq!(f.count("notifications").await, 0);
    assert!(f.m.pushes.lock().unwrap().is_empty());
}
