use axum::{
    Json, Router,
    body::Body,
    extract::State,
    http::{Request, StatusCode},
    response::IntoResponse,
    routing::{get, post, put},
};
use http_body_util::BodyExt;
use lingxi::{
    auth, db, integrations, memory,
    qq::{self, store, vector, worker},
    settings,
    state::App,
};
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};
use tokio_util::sync::CancellationToken;
use tower::ServiceExt;

#[derive(Clone, Default)]
struct Mock {
    points: Arc<Mutex<Vec<Value>>>,
    requests: Arc<Mutex<Vec<Value>>>,
    sends: Arc<Mutex<Vec<Value>>>,
    model_requests: Arc<Mutex<Vec<Value>>>,
    decision_requests: Arc<Mutex<Vec<Value>>>,
    decision_response: Arc<Mutex<Option<String>>>,
    decision_delay: Arc<std::sync::atomic::AtomicU64>,
    fail_send: Arc<Mutex<bool>>,
    fail_after: Arc<std::sync::atomic::AtomicUsize>,
    model_response: Arc<Mutex<Option<String>>>,
    hold: Arc<std::sync::atomic::AtomicBool>,
    entered: Arc<tokio::sync::Notify>,
    release: Arc<tokio::sync::Notify>,
}
async fn embedding() -> Json<Value> {
    Json(json!({"data":[{"embedding":[1.0,0.0,0.0]}]}))
}
async fn decision(State(s): State<Mock>, Json(v): Json<Value>) -> Json<Value> {
    s.decision_requests.lock().unwrap().push(v);
    tokio::time::sleep(std::time::Duration::from_millis(
        s.decision_delay.load(std::sync::atomic::Ordering::Relaxed),
    ))
    .await;
    let content = s
        .decision_response
        .lock()
        .unwrap()
        .clone()
        .unwrap_or_else(|| r#"{"action":"reply"}"#.into());
    Json(json!({"done":true,"done_reason":"stop","message":{"content":content}}))
}
async fn info() -> Json<Value> {
    Json(json!({"result":{"config":{"params":{"vectors":{"size":3}}}}}))
}
async fn upsert(State(s): State<Mock>, Json(v): Json<Value>) -> Json<Value> {
    s.points
        .lock()
        .unwrap()
        .extend(v["points"].as_array().unwrap().clone());
    Json(json!({"status":"ok","result":{"status":"completed"}}))
}
async fn query(State(s): State<Mock>, Json(v): Json<Value>) -> Json<Value> {
    s.requests.lock().unwrap().push(v);
    Json(json!({"result":{"points":s.points.lock().unwrap().clone()}}))
}
async fn delete(State(s): State<Mock>, Json(v): Json<Value>) -> Json<Value> {
    s.points
        .lock()
        .unwrap()
        .retain(|p| !v["points"].as_array().unwrap().contains(&p["id"]));
    Json(json!({"status":"ok","result":{"status":"completed"}}))
}
async fn model(State(s): State<Mock>, Json(v): Json<Value>) -> impl IntoResponse {
    let wait = v["model"] == "mock" && s.hold.load(std::sync::atomic::Ordering::SeqCst);
    s.model_requests.lock().unwrap().push(v);
    if wait {
        s.entered.notify_one();
        s.release.notified().await;
    }
    let content = s
        .model_response
        .lock()
        .unwrap()
        .clone()
        .unwrap_or_else(|| "我在，欢迎聊聊今天的计划。".into());
    (
        [("content-type", "text/event-stream")],
        format!(
            "data: {}\n\ndata: [DONE]\n\n",
            json!({"choices":[{"delta":{"content":content}}]})
        ),
    )
}
async fn send(State(s): State<Mock>, Json(v): Json<Value>) -> Json<Value> {
    s.sends.lock().unwrap().push(v);
    let threshold = s.fail_after.load(std::sync::atomic::Ordering::Relaxed);
    Json(
        if *s.fail_send.lock().unwrap()
            || (threshold > 0 && s.sends.lock().unwrap().len() > threshold)
        {
            json!({"status":"failed","retcode":100})
        } else {
            json!({"status":"ok","retcode":0,"data":{"message_id":987}})
        },
    )
}
struct Fixture {
    app: App,
    user: auth::User,
    mock: Mock,
    server: tokio::task::JoinHandle<()>,
    _temp: tempfile::TempDir,
}
impl Drop for Fixture {
    fn drop(&mut self) {
        self.server.abort()
    }
}
impl Fixture {
    async fn api(
        &self,
        user: &auth::User,
        method: &str,
        path: &str,
        body: Value,
    ) -> (StatusCode, Value) {
        let token = auth::issue_token(&self.app, user, "group-test", 3600)
            .await
            .unwrap();
        let req = Request::builder()
            .method(method)
            .uri(path)
            .header("Authorization", format!("Bearer {token}"))
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .unwrap();
        let response = lingxi::api::router(self.app.clone())
            .oneshot(req)
            .await
            .unwrap();
        let status = response.status();
        let value =
            serde_json::from_slice(&response.into_body().collect().await.unwrap().to_bytes())
                .unwrap();
        (status, value)
    }
    async fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let pool = db::connect(&temp.path().join("main.sqlite3"))
            .await
            .unwrap();
        let app = App::new(
            pool,
            db::Secrets::from_key(&[5; 32]),
            temp.path().into(),
            true,
        )
        .unwrap();
        let user = auth::create_user(&app, "owner", "test-qq-password-123".into(), false)
            .await
            .unwrap();
        let mock = Mock::default();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let router = Router::new()
            .route("/v1/embeddings", post(embedding))
            .route("/v1/chat/completions", post(model))
            .route("/api/chat", post(decision))
            .route("/send_group_msg", post(send))
            .route("/collections/{name}", get(info))
            .route("/collections/{name}/points", put(upsert))
            .route("/collections/{name}/points/query", post(query))
            .route("/collections/{name}/points/delete", post(delete))
            .with_state(mock.clone());
        let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        settings::save_profiles(
            &app,
            &user,
            vec![settings::Profile {
                id: "mock".into(),
                name: "mock".into(),
                protocol: "openai".into(),
                base_url: format!("{base}/v1"),
                api_key: "test-key".into(),
                model: "mock".into(),
                context_window: 32000,
                max_output: 2048,
                system_prompt: "PRIVATE SYSTEM PROMPT".into(),
            }],
        )
        .await
        .unwrap();
        integrations::save_config(&app,&user,json!({"qq":{"enabled":true,"owner_qq":"10001","bot_qq":"20001","groups":["30001","30002"],"inbound":{"secret":"test-qq-inbound-secret-at-least-32-chars"},"onebot":{"url":base,"token":"test"},"vector":{"url":base},"embedding":{"url":format!("{base}/v1"),"model":"mock-embed","api_key":"test"},"decision":{"aliases":["灵犀","大肥鱼","鱼鱼"]},"collect_memory":false}})).await.unwrap();
        Self {
            app,
            user,
            mock,
            server,
            _temp: temp,
        }
    }
    fn message(&self, id: i64, qq: &str, group: &str, text: &str, mention: bool) -> Value {
        let mut message = vec![json!({"type":"text","data":{"text":text}})];
        if mention {
            message.push(json!({"type":"at","data":{"qq":"20001"}}))
        };
        json!({"post_type":"message","message_type":"group","self_id":20001,"message_id":id,"group_id":group,"user_id":qq,"sender":{"nickname":"我是主人"},"message":message,"time":db::now()})
    }
    async fn ingest(&self, id: i64, qq: &str, group: &str, text: &str, mention: bool) -> Value {
        qq::api::ingest(
            &self.app,
            &self.user,
            &self.message(id, qq, group, text, mention),
        )
        .await
        .unwrap()
    }
    async fn fact(&self, qq: &str, group: &str, content: &str) -> String {
        store::save_fact(
            &self.app,
            &self.user,
            &store::FactInput {
                group_id: group.into(),
                qq_id: qq.into(),
                name: "测试成员".into(),
                content: content.into(),
                expires_at: None,
            },
            "manual",
            1.0,
            &json!([]),
        )
        .await
        .unwrap()
    }
    async fn decision_mode(&self, mode: &str) {
        let mut cfg = integrations::config(&self.app, &self.user).await.unwrap();
        cfg["qq"]["decision"] = json!({"url":cfg["qq"]["onebot"]["url"],"timeout_secs":3,"aliases":["灵犀","大肥鱼","鱼鱼"]});
        cfg["qq"]["group_settings"] =
            json!({"30001":{"decision":{"mode":mode,"max_replies_per_hour":1}}});
        integrations::save_config(&self.app, &self.user, cfg)
            .await
            .unwrap();
    }
    async fn process(&self, event: &Value) -> Value {
        worker::process(
            &self.app,
            &self.user,
            &json!({"id":event["event_id"],"owner":event["storage"]=="main"}),
            &CancellationToken::new(),
        )
        .await
        .unwrap()
    }
}

#[tokio::test]
async fn local_decision_observes_without_reply_and_only_sees_current_group() {
    let f = Fixture::new().await;
    f.decision_mode("observe").await;
    f.fact("10001", "30001", "PRIVATE OWNER MEMORY").await;
    f.ingest(300, "10002", "30002", "OTHER GROUP CONTENT", false)
        .await;
    f.ingest(301, "10001", "30001", "本群主人公开发言", false)
        .await;
    let event = f
        .ingest(302, "10002", "30001", "想问大肥鱼喜欢什么？", false)
        .await;
    assert_eq!(f.process(&event).await["delivered"], false);
    assert!(f.mock.sends.lock().unwrap().is_empty());
    assert!(f.mock.model_requests.lock().unwrap().is_empty());
    let requests = f.mock.decision_requests.lock().unwrap();
    assert_eq!(requests.len(), 1);
    let body = requests[0].to_string();
    assert!(body.contains("本群主人公开发言"));
    assert!(!body.contains("OTHER GROUP CONTENT"));
    assert!(!body.contains("PRIVATE OWNER MEMORY"));
    assert!(!body.contains("PRIVATE SYSTEM PROMPT"));
    assert_eq!(requests[0]["think"], false);
    drop(requests);
    let (_, view) = f
        .api(
            &f.user,
            "GET",
            "/api/v2/qq/groups/30001/conversation",
            json!({}),
        )
        .await;
    assert_eq!(view["data"]["decisions"][0]["reason"], "observe");
    assert_eq!(view["data"]["decisions"][0]["authorized"], false);
}

#[tokio::test]
async fn local_decision_auto_replies_once_then_enforces_group_budget() {
    let f = Fixture::new().await;
    f.decision_mode("auto").await;
    let event = f.ingest(310, "10001", "30001", "大肥鱼来聊聊", false).await;
    assert_eq!(f.process(&event).await["delivered"], true);
    f.process(&event).await;
    let next = f.ingest(311, "10002", "30001", "你再说说", false).await;
    assert_eq!(f.process(&next).await["delivered"], false);
    let other = f.ingest(312, "10002", "30002", "大肥鱼来聊聊", false).await;
    assert_eq!(f.process(&other).await["delivered"], false);
    assert_eq!(f.mock.decision_requests.lock().unwrap().len(), 1);
    assert_eq!(f.mock.sends.lock().unwrap().len(), 1);
    let (_, view) = f
        .api(
            &f.user,
            "GET",
            "/api/v2/qq/groups/30001/conversation",
            json!({}),
        )
        .await;
    assert_eq!(view["data"]["decisions"][0]["reason"], "reply_limit");
    let owner_in_member_db: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM events WHERE qq_id='10001'")
            .fetch_one(store::pool(&f.app).await.unwrap())
            .await
            .unwrap();
    assert_eq!(owner_in_member_db, 0);
}

#[tokio::test]
async fn local_decision_wait_ignore_invalid_and_timeout_never_send() {
    for response in [
        r#"{"action":"wait"}"#,
        r#"{"action":"ignore"}"#,
        "not JSON",
        r#"{"action":"reply","confidence":1}"#,
        "timeout",
    ] {
        let f = Fixture::new().await;
        f.decision_mode("auto").await;
        *f.mock.decision_response.lock().unwrap() = Some(response.into());
        if response == "timeout" {
            f.mock
                .decision_delay
                .store(4000, std::sync::atomic::Ordering::Relaxed);
        }
        let event = f.ingest(320, "10002", "30001", "大肥鱼等等", false).await;
        assert_eq!(f.process(&event).await["delivered"], false);
        assert!(f.mock.sends.lock().unwrap().is_empty());
        assert!(f.mock.model_requests.lock().unwrap().is_empty());
    }
}

#[tokio::test]
async fn direct_mentions_bypass_unavailable_classifier_and_stale_messages_do_not_infer() {
    let f = Fixture::new().await;
    f.decision_mode("auto").await;
    *f.mock.decision_response.lock().unwrap() = Some("broken".into());
    let event = f.ingest(330, "10002", "30001", "你好", true).await;
    assert_eq!(f.process(&event).await["delivered"], true);
    assert!(f.mock.decision_requests.lock().unwrap().is_empty());
    let stale = f.ingest(331, "10002", "30001", "旧消息", false).await;
    sqlx::query("UPDATE events SET occurred_at=? WHERE id=?")
        .bind(db::now() - 300)
        .bind(stale["event_id"].as_str().unwrap())
        .execute(store::pool(&f.app).await.unwrap())
        .await
        .unwrap();
    assert_eq!(f.process(&stale).await["delivered"], false);
    assert!(f.mock.decision_requests.lock().unwrap().is_empty());
}

#[tokio::test]
async fn turning_off_automatic_replies_during_generation_prevents_send() {
    let f = Fixture::new().await;
    f.decision_mode("auto").await;
    let event = f
        .ingest(340, "10002", "30001", "大肥鱼帮我想想", false)
        .await;
    f.mock.hold.store(true, std::sync::atomic::Ordering::SeqCst);
    let (app, user) = (f.app.clone(), f.user.clone());
    let task = tokio::spawn(async move {
        worker::process(
            &app,
            &user,
            &json!({"id":event["event_id"],"owner":false}),
            &CancellationToken::new(),
        )
        .await
    });
    tokio::time::timeout(std::time::Duration::from_secs(3), f.mock.entered.notified())
        .await
        .unwrap();
    f.decision_mode("off").await;
    f.mock.release.notify_one();
    assert_eq!(
        task.await.unwrap().unwrap()["ignored"],
        "group_settings_changed"
    );
    assert!(f.mock.sends.lock().unwrap().is_empty());
}

#[tokio::test]
async fn local_decision_rejects_public_endpoints_and_probe_does_not_send() {
    let f = Fixture::new().await;
    let mut cfg = integrations::config(&f.app, &f.user).await.unwrap();
    cfg["qq"]["decision"] = json!({"url":"http://8.8.8.8:11434"});
    cfg["qq"]["group_settings"] = json!({"30001":{"decision":{"mode":"observe"}}});
    assert!(
        integrations::save_config(&f.app, &f.user, cfg)
            .await
            .is_err()
    );
    f.decision_mode("observe").await;
    let (status, result) = f
        .api(
            &f.user,
            "POST",
            "/api/v2/qq/decision/probe",
            json!({"text":"想听听你的建议"}),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(result["data"]["action"], "reply");
    assert_eq!(result["data"]["sent"], false);
    assert!(f.mock.sends.lock().unwrap().is_empty());
}

#[tokio::test]
async fn explicit_address_rules_work_without_inference_and_obey_the_reply_budget() {
    let f = Fixture::new().await;
    f.decision_mode("auto").await;
    *f.mock.decision_response.lock().unwrap() = Some("unavailable".into());
    let waiting = f
        .ingest(350, "10002", "30001", "鱼鱼，等我说完", false)
        .await;
    assert_eq!(f.process(&waiting).await["delivered"], false);
    let quiet = f
        .ingest(351, "10002", "30001", "鱼鱼，先别回复", false)
        .await;
    assert_eq!(f.process(&quiet).await["delivered"], false);
    let addressed = f
        .ingest(352, "10002", "30001", "大肥鱼，你喜欢什么颜色呀？", false)
        .await;
    assert_eq!(f.process(&addressed).await["delivered"], true);
    let again = f
        .ingest(353, "10002", "30001", "大肥鱼，你还在吗？", false)
        .await;
    assert_eq!(f.process(&again).await["delivered"], false);
    assert!(f.mock.decision_requests.lock().unwrap().is_empty());
    assert_eq!(f.mock.sends.lock().unwrap().len(), 1);
}

async fn rhythm_config(f: &Fixture, style: bool, followup: bool, proactive: bool) {
    let mut cfg = integrations::config(&f.app, &f.user).await.unwrap();
    cfg["qq"]["group_settings"] = json!({"30001":{"system_prompt":"PUBLIC_GROUP_PERSONA","reply_style":{"enabled":style,"max_messages":3,"interval_secs":1},"followup":{"enabled":followup,"delay_minutes":1,"max_followups":2,"daily_limit":4},"proactive":{"enabled":proactive,"active_start":"00:00","active_end":"00:00","quiet_start":"00:00","quiet_end":"00:00","idle_minutes":5}}});
    integrations::save_config(&f.app, &f.user, cfg)
        .await
        .unwrap();
}
async fn due_outreach(f: &Fixture) -> String {
    let pool = store::pool(&f.app).await.unwrap();
    let key: String =
        sqlx::query_scalar("SELECT id FROM outreach WHERE status='pending' ORDER BY rowid LIMIT 1")
            .fetch_one(pool)
            .await
            .unwrap();
    sqlx::query("UPDATE outreach SET due_at=? WHERE id=?")
        .bind(db::now() - 1)
        .bind(&key)
        .execute(pool)
        .await
        .unwrap();
    key
}
async fn run_outreach(f: &Fixture, key: &str) -> Value {
    qq::outreach::process(
        &f.app,
        &f.user,
        &json!({"id":key,"private":false}),
        &CancellationToken::new(),
    )
    .await
    .unwrap()
}
#[tokio::test]
async fn multipart_tracks_each_send_and_never_replays_uncertain_parts() {
    let f = Fixture::new().await;
    rhythm_config(&f, true, false, false).await;
    *f.mock.model_response.lock().unwrap() = Some("第一条\n\n第二条\n\n第三条".into());
    f.mock
        .fail_after
        .store(1, std::sync::atomic::Ordering::Relaxed);
    let event = f.ingest(700, "10002", "30001", "鱼来聊聊", true).await;
    let params = json!({"id":event["event_id"],"owner":false});
    assert!(
        worker::process(&f.app, &f.user, &params, &CancellationToken::new())
            .await
            .is_err()
    );
    let parts = qq::dialogue::parts(&f.app, &f.user, event["event_id"].as_str().unwrap(), false)
        .await
        .unwrap();
    assert_eq!(
        parts
            .iter()
            .map(|p| p["status"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["sent", "unknown", "cancelled"]
    );
    let sends = f.mock.sends.lock().unwrap().clone();
    assert_eq!(sends.len(), 2);
    assert_eq!(sends[0]["message"][0]["type"], "at");
    assert_eq!(sends[1]["message"][0]["type"], "text");
    assert!(
        worker::process(&f.app, &f.user, &params, &CancellationToken::new())
            .await
            .is_err()
    );
    assert_eq!(f.mock.sends.lock().unwrap().len(), 2);
}
#[tokio::test]
async fn model_chooses_followup_count_and_skip_ends_round() {
    let f = Fixture::new().await;
    rhythm_config(&f, false, true, false).await;
    let event = f.ingest(701, "10002", "30001", "摄影话题", true).await;
    worker::process(
        &f.app,
        &f.user,
        &json!({"id":event["event_id"],"owner":false}),
        &CancellationToken::new(),
    )
    .await
    .unwrap();
    *f.mock.model_response.lock().unwrap()=Some(json!({"decision":"send","message":"还想到一个构图的小办法。","source_ids":[],"continue":true}).to_string());
    let first = due_outreach(&f).await;
    assert_eq!(run_outreach(&f, &first).await["status"], "sent");
    let second = due_outreach(&f).await;
    *f.mock.model_response.lock().unwrap() =
        Some(json!({"decision":"skip","message":"","source_ids":[],"continue":true}).to_string());
    assert_eq!(run_outreach(&f, &second).await["status"], "skip");
    let pending: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM outreach WHERE status='pending'")
        .fetch_one(store::pool(&f.app).await.unwrap())
        .await
        .unwrap();
    assert_eq!(pending, 0);
    assert_eq!(f.mock.sends.lock().unwrap().len(), 2);
    let all = f
        .mock
        .model_requests
        .lock()
        .unwrap()
        .last()
        .unwrap()
        .to_string();
    assert!(all.contains("PUBLIC_GROUP_PERSONA"));
    assert!(!all.contains("PRIVATE SYSTEM PROMPT"));
    assert!(all.contains("构图"));
}
#[tokio::test]
async fn followup_round_cap_and_incoming_messages_stop_continuation() {
    let f = Fixture::new().await;
    rhythm_config(&f, false, true, false).await;
    f.ingest(702, "10002", "30001", "聊摄影", false).await;
    let cfg = qq::config(&f.app, &f.user)
        .await
        .unwrap()
        .for_group("30001")
        .unwrap();
    qq::outreach::schedule_followup(&f.app, &f.user, &cfg, Some("30001"), "round")
        .await
        .unwrap();
    for n in 1..=2 {
        *f.mock.model_response.lock().unwrap()=Some(json!({"decision":"send","message":format!("第{n}个不一样的新想法"),"source_ids":[],"continue":true}).to_string());
        let key = due_outreach(&f).await;
        assert_eq!(run_outreach(&f, &key).await["status"], "sent");
        run_outreach(&f, &key).await;
    }
    assert_eq!(f.mock.sends.lock().unwrap().len(), 2);
    let pending: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM outreach WHERE status='pending'")
        .fetch_one(store::pool(&f.app).await.unwrap())
        .await
        .unwrap();
    assert_eq!(pending, 0);
    qq::outreach::schedule_followup(&f.app, &f.user, &cfg, Some("30001"), "next-round")
        .await
        .unwrap();
    f.ingest(703, "10001", "30001", "我来接着聊", false).await;
    let key = due_outreach(&f).await;
    assert_eq!(run_outreach(&f, &key).await["reason"], "new_message");
    assert_eq!(f.mock.sends.lock().unwrap().len(), 2);
}
#[tokio::test]
async fn incoming_during_followup_model_generation_cancels_send() {
    let f = Fixture::new().await;
    rhythm_config(&f, false, true, false).await;
    f.ingest(704, "10002", "30001", "摄影", false).await;
    let cfg = qq::config(&f.app, &f.user)
        .await
        .unwrap()
        .for_group("30001")
        .unwrap();
    qq::outreach::schedule_followup(&f.app, &f.user, &cfg, Some("30001"), "race")
        .await
        .unwrap();
    let key = due_outreach(&f).await;
    *f.mock.model_response.lock().unwrap() = Some(
        json!({"decision":"send","message":"补充一个构图方法","source_ids":[],"continue":true})
            .to_string(),
    );
    f.mock
        .hold
        .store(true, std::sync::atomic::Ordering::Relaxed);
    let app = f.app.clone();
    let user = f.user.clone();
    let task = tokio::spawn(async move {
        qq::outreach::process(
            &app,
            &user,
            &json!({"id":key,"private":false}),
            &CancellationToken::new(),
        )
        .await
        .unwrap()
    });
    f.mock.entered.notified().await;
    f.ingest(705, "10002", "30001", "我已经想到办法啦", false)
        .await;
    f.mock.release.notify_one();
    assert_eq!(task.await.unwrap()["reason"], "new_message");
    assert!(f.mock.sends.lock().unwrap().is_empty());
}
#[tokio::test]
async fn proactive_schedule_waits_then_uses_only_group_context_and_awaits_response() {
    let f = Fixture::new().await;
    rhythm_config(&f, false, false, true).await;
    f.ingest(706, "10002", "30001", "本群摄影计划", false).await;
    f.ingest(707, "10003", "30002", "OTHER_GROUP_SECRET", false)
        .await;
    sqlx::query("UPDATE events SET created_at=?,occurred_at=? WHERE group_id='30001'")
        .bind(db::now() - 600)
        .bind(db::now() - 600)
        .execute(store::pool(&f.app).await.unwrap())
        .await
        .unwrap();
    qq::outreach::reconcile(&f.app).await.unwrap();
    assert!(f.mock.sends.lock().unwrap().is_empty());
    let pool = store::pool(&f.app).await.unwrap();
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM outreach")
        .fetch_one(pool)
        .await
        .unwrap();
    assert_eq!(count, 0);
    sqlx::query("UPDATE outreach_state SET next_check_at=0")
        .execute(pool)
        .await
        .unwrap();
    qq::outreach::reconcile(&f.app).await.unwrap();
    *f.mock.model_response.lock().unwrap()=Some(json!({"decision":"send","message":"大家想试试哪种构图？","source_ids":[],"continue":false}).to_string());
    let key = due_outreach(&f).await;
    assert_eq!(run_outreach(&f, &key).await["status"], "sent");
    let req = f
        .mock
        .model_requests
        .lock()
        .unwrap()
        .last()
        .unwrap()
        .to_string();
    assert!(req.contains("本群摄影计划"));
    assert!(!req.contains("OTHER_GROUP_SECRET"));
    assert!(!req.contains("PRIVATE SYSTEM PROMPT"));
    let first = f.mock.sends.lock().unwrap()[0].clone();
    assert_eq!(first["message"][0]["type"], "text");
    sqlx::query("INSERT INTO outreach(id,user_id,group_id,kind,origin,anchor,config_key,due_at,created_at,updated_at) SELECT 'again',user_id,group_id,kind,'again',anchor,config_key,?,created_at,updated_at FROM outreach WHERE id=?").bind(db::now()-1).bind(&key).execute(pool).await.unwrap();
    assert_eq!(run_outreach(&f, "again").await["reason"], "awaiting_reply");
    assert_eq!(f.mock.sends.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn identity_routes_owner_to_main_and_everyone_else_to_separate_db() {
    let f = Fixture::new().await;
    let owner = f
        .ingest(1, "10001", "30001", "主人喜欢安静思考", false)
        .await;
    let member = f
        .ingest(2, "10002", "30001", "群成员喜欢热闹讨论", false)
        .await;
    assert_eq!(owner["storage"], "main");
    assert_eq!(member["storage"], "qq");
    let messages: Vec<String> = sqlx::query_scalar("SELECT content FROM messages")
        .fetch_all(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(messages, vec!["主人喜欢安静思考"]);
    let other: Vec<String> = sqlx::query_scalar("SELECT content FROM events")
        .fetch_all(store::pool(&f.app).await.unwrap())
        .await
        .unwrap();
    assert_eq!(other, vec!["群成员喜欢热闹讨论"]);
    let owner_fact = f.fact("10001", "30001", "主人偏好简明回答").await;
    let updated = f.fact("10001", "30001", "主人偏好 简明回答").await;
    assert_eq!(owner_fact, updated);
    assert_eq!(
        memory::list(&f.app, &f.user).await.unwrap()[0].id,
        owner_fact
    );
    let id = f.fact("10002", "30001", "成员交流时喜欢举例").await;
    assert_eq!(memory::list(&f.app, &f.user).await.unwrap().len(), 1);
    assert_eq!(
        store::facts(&f.app, &f.user, "30001", "10001")
            .await
            .unwrap()
            .len(),
        0
    );
    assert_eq!(
        store::facts(&f.app, &f.user, "30001", "10002")
            .await
            .unwrap()[0]
            .id,
        id
    );
    assert!(f.app.data_dir.join("qq/people.sqlite3").exists());
    let jobs: Vec<String> = sqlx::query_scalar("SELECT params FROM jobs")
        .fetch_all(&f.app.pool)
        .await
        .unwrap();
    assert!(!jobs.join("").contains("群成员喜欢"));
}
#[tokio::test]
async fn inbound_auth_whitelist_self_messages_and_duplicate_delivery() {
    let f = Fixture::new().await;
    let path = format!("/api/v2/qq/inbound/{}", f.user.id);
    let response = lingxi::api::router(f.app.clone())
        .oneshot(
            Request::builder()
                .uri(&path)
                .method("POST")
                .header("content-type", "application/json")
                .body(Body::from(
                    f.message(1, "10002", "30001", "你好世界", false)
                        .to_string(),
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    assert_eq!(
        f.ingest(1, "10002", "99999", "不接收的群", false).await["ignored"],
        true
    );
    assert_eq!(
        f.ingest(2, "20001", "30001", "机器人自己的消息", false)
            .await["ignored"],
        true
    );
    let first = f
        .ingest(3, "10002", "30001", "同一消息只保存一次", false)
        .await;
    let second = f
        .ingest(3, "10002", "30001", "同一消息只保存一次", false)
        .await;
    assert_eq!(first, second);
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM events")
        .fetch_one(store::pool(&f.app).await.unwrap())
        .await
        .unwrap();
    assert_eq!(count, 1);
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM jobs WHERE action='qq_event'")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(count, 1);
}
#[tokio::test]
async fn vector_retrieval_enforces_tenant_group_person_and_tombstones() {
    let f = Fixture::new().await;
    let own = f.fact("10002", "30001", "喜欢直接说明结论").await;
    let other_group = f.fact("10002", "30002", "另一个群里偏好长讨论").await;
    let other_person = f.fact("10003", "30001", "其他人喜欢分享图片").await;
    for id in [&own, &other_group, &other_person] {
        vector::index(&f.app, &f.user, id).await.unwrap()
    }
    let recalled = vector::recall(&f.app, &f.user, "30001", "10002", "沟通风格")
        .await
        .unwrap();
    assert_eq!(recalled.len(), 1);
    assert_eq!(recalled[0].id, own);
    let request = f.mock.requests.lock().unwrap()[0].clone();
    assert_eq!(request["filter"]["must"][0]["match"]["value"], f.user.id);
    assert_eq!(request["filter"]["must"][1]["match"]["value"], "30001");
    assert_eq!(request["filter"]["must"][2]["match"]["value"], "10002");
    store::forget(&f.app, &f.user, &own).await.unwrap();
    assert!(
        vector::recall(&f.app, &f.user, "30001", "10002", "沟通风格")
            .await
            .unwrap()
            .is_empty()
    );
    vector::index(&f.app, &f.user, &own).await.unwrap();
    assert!(!f.mock.points.lock().unwrap().iter().any(|p| p["id"] == own));
    let other = auth::create_user(&f.app, "another", "other-qq-password-123".into(), false)
        .await
        .unwrap();
    assert!(store::fact(&f.app, &other, &other_person).await.is_err());
    assert!(
        vector::recall(&f.app, &f.user, "30001", "10001", "主人特点")
            .await
            .unwrap()
            .is_empty()
    );
}
#[tokio::test]
async fn group_management_creates_stable_unique_conversations_and_preserves_history() {
    let f = Fixture::new().await;
    let (_, first) = f.api(&f.user, "GET", "/api/v2/qq/groups", json!({})).await;
    let (_, second) = f.api(&f.user, "GET", "/api/v2/qq/groups", json!({})).await;
    assert_eq!(first["data"].as_array().unwrap().len(), 2);
    assert_ne!(
        first["data"][0]["conversation"]["id"],
        first["data"][1]["conversation"]["id"]
    );
    assert_eq!(
        first["data"][0]["conversation"],
        second["data"][0]["conversation"]
    );
    let body =
        json!({"group_id":"30003","settings":{"name":"摄影群","system_prompt":"温和聊摄影"}});
    let (status, created) = f
        .api(&f.user, "POST", "/api/v2/qq/groups", body.clone())
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        f.api(&f.user, "POST", "/api/v2/qq/groups", body.clone())
            .await
            .0,
        StatusCode::CONFLICT
    );
    f.ingest(100, "10002", "30003", "第三个群的历史", false)
        .await;
    let (_, view) = f
        .api(
            &f.user,
            "GET",
            "/api/v2/qq/groups/30003/conversation",
            json!({}),
        )
        .await;
    assert_eq!(view["data"]["history"][0]["content"], "第三个群的历史");
    assert_eq!(
        f.api(&f.user, "DELETE", "/api/v2/qq/groups/30003", json!({}))
            .await
            .0,
        StatusCode::OK
    );
    assert_eq!(
        f.ingest(101, "10002", "30003", "移除后忽略", true).await["ignored"],
        true
    );
    let (_, again) = f.api(&f.user, "POST", "/api/v2/qq/groups", body).await;
    assert_eq!(again["data"]["id"], created["data"]["id"]);
    let (_, view) = f
        .api(
            &f.user,
            "GET",
            "/api/v2/qq/groups/30003/conversation",
            json!({}),
        )
        .await;
    assert_eq!(view["data"]["history"].as_array().unwrap().len(), 1);
    let other = auth::create_user(&f.app, "other", "other-test-password".into(), false)
        .await
        .unwrap();
    assert_eq!(
        f.api(
            &other,
            "GET",
            "/api/v2/qq/groups/30003/conversation",
            json!({})
        )
        .await
        .0,
        StatusCode::NOT_FOUND
    );
    assert!(
        f.api(&other, "GET", "/api/v2/qq/groups", json!({})).await.1["data"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}
#[tokio::test]
async fn independent_group_personas_models_and_reply_history_do_not_leak() {
    let f = Fixture::new().await;
    let mut profiles = settings::profiles(&f.app, &f.user, true).await.unwrap();
    let mut second = profiles[0].clone();
    second.id = "second".into();
    second.model = "second-model".into();
    profiles.push(second);
    settings::save_profiles(&f.app, &f.user, profiles)
        .await
        .unwrap();
    assert_eq!(
        f.api(
            &f.user,
            "PUT",
            "/api/v2/qq/groups/30001",
            json!({"name":"群A","system_prompt":"GROUP_A_PERSONA"})
        )
        .await
        .0,
        StatusCode::OK
    );
    assert_eq!(
        f.api(
            &f.user,
            "PUT",
            "/api/v2/qq/groups/30002",
            json!({"name":"群B","profile_id":"second","system_prompt":"GROUP_B_PERSONA"})
        )
        .await
        .0,
        StatusCode::OK
    );
    f.ingest(102, "10002", "30002", "ONLY_GROUP_B_HISTORY", false)
        .await;
    let a = f
        .ingest(103, "10002", "30001", "ONLY_GROUP_A_QUESTION", true)
        .await;
    *f.mock.model_response.lock().unwrap() = Some("ONLY_GROUP_A_REPLY".into());
    worker::process(
        &f.app,
        &f.user,
        &json!({"id":a["event_id"],"owner":false}),
        &CancellationToken::new(),
    )
    .await
    .unwrap();
    let a2 = f
        .ingest(104, "10002", "30001", "接着上一次回答继续", true)
        .await;
    sqlx::query("UPDATE qq_reply_limits SET last_at=0")
        .execute(&f.app.pool)
        .await
        .unwrap();
    worker::process(
        &f.app,
        &f.user,
        &json!({"id":a2["event_id"],"owner":false}),
        &CancellationToken::new(),
    )
    .await
    .unwrap();
    let b = f.ingest(105, "10002", "30002", "B群的问题", true).await;
    worker::process(
        &f.app,
        &f.user,
        &json!({"id":b["event_id"],"owner":false}),
        &CancellationToken::new(),
    )
    .await
    .unwrap();
    let requests = f.mock.model_requests.lock().unwrap();
    assert_eq!(requests[0]["model"], "mock");
    assert_eq!(requests[2]["model"], "second-model");
    assert!(requests[0].to_string().contains("GROUP_A_PERSONA"));
    assert!(!requests[0].to_string().contains("GROUP_B"));
    assert!(requests[1].to_string().contains("ONLY_GROUP_A_REPLY"));
    assert!(requests[2].to_string().contains("ONLY_GROUP_B_HISTORY"));
    assert!(!requests[2].to_string().contains("ONLY_GROUP_A"));
    assert!(!requests[2].to_string().contains("PRIVATE SYSTEM"));
    drop(requests);
    let sent = f.mock.sends.lock().unwrap();
    assert_eq!(
        sent.iter()
            .map(|v| v["group_id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        vec!["30001", "30001", "30002"]
    );
}
#[tokio::test]
async fn group_leases_serialize_duplicates_without_blocking_another_group() {
    let f = Fixture::new().await;
    let mut profiles = settings::profiles(&f.app, &f.user, true).await.unwrap();
    let mut second = profiles[0].clone();
    second.id = "second".into();
    second.model = "fast".into();
    profiles.push(second);
    settings::save_profiles(&f.app, &f.user, profiles)
        .await
        .unwrap();
    f.api(
        &f.user,
        "PUT",
        "/api/v2/qq/groups/30002",
        json!({"profile_id":"second"}),
    )
    .await;
    let a = f.ingest(106, "10002", "30001", "群A慢回复", true).await;
    let b = f.ingest(107, "10002", "30002", "群B独立回复", true).await;
    f.mock.hold.store(true, std::sync::atomic::Ordering::SeqCst);
    let params = json!({"id":a["event_id"],"owner":false});
    let (app, user, p) = (f.app.clone(), f.user.clone(), params.clone());
    let first =
        tokio::spawn(
            async move { worker::process(&app, &user, &p, &CancellationToken::new()).await },
        );
    tokio::time::timeout(std::time::Duration::from_secs(3), f.mock.entered.notified())
        .await
        .unwrap();
    let (app, user, p) = (f.app.clone(), f.user.clone(), params);
    let duplicate =
        tokio::spawn(
            async move { worker::process(&app, &user, &p, &CancellationToken::new()).await },
        );
    tokio::time::timeout(
        std::time::Duration::from_secs(3),
        worker::process(
            &f.app,
            &f.user,
            &json!({"id":b["event_id"],"owner":false}),
            &CancellationToken::new(),
        ),
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(f.mock.sends.lock().unwrap().len(), 1);
    assert!(!duplicate.is_finished());
    f.mock
        .hold
        .store(false, std::sync::atomic::Ordering::SeqCst);
    f.mock.release.notify_one();
    first.await.unwrap().unwrap();
    duplicate.await.unwrap().unwrap();
    assert_eq!(f.mock.sends.lock().unwrap().len(), 2);
    assert_eq!(f.mock.model_requests.lock().unwrap().len(), 2);
}
#[tokio::test]
async fn paused_group_ignores_input_and_disable_during_generation_prevents_send() {
    let f = Fixture::new().await;
    let a = f
        .ingest(108, "10002", "30001", "先生成，再暂停", true)
        .await;
    f.mock.hold.store(true, std::sync::atomic::Ordering::SeqCst);
    let (app, user) = (f.app.clone(), f.user.clone());
    let first = tokio::spawn(async move {
        worker::process(
            &app,
            &user,
            &json!({"id":a["event_id"],"owner":false}),
            &CancellationToken::new(),
        )
        .await
    });
    tokio::time::timeout(std::time::Duration::from_secs(3), f.mock.entered.notified())
        .await
        .unwrap();
    assert_eq!(
        f.api(
            &f.user,
            "PUT",
            "/api/v2/qq/groups/30001",
            json!({"enabled":false})
        )
        .await
        .0,
        StatusCode::OK
    );
    assert_eq!(
        f.ingest(109, "10002", "30001", "暂停后不接收", true).await["ignored"],
        true
    );
    assert_eq!(
        f.ingest(110, "10002", "30002", "其他群仍接收", false).await["accepted"],
        true
    );
    f.mock.release.notify_one();
    assert_eq!(
        first.await.unwrap().unwrap()["ignored"],
        "group_settings_changed"
    );
    assert!(f.mock.sends.lock().unwrap().is_empty());
}
#[tokio::test]
async fn group_reply_never_loads_owner_private_history_or_tools_and_sends_once() {
    let f = Fixture::new().await;
    memory::remember(
        &f.app,
        &f.user,
        &memory::Input {
            content: "PRIVATE_OWNER_SECRET_771122".into(),
            importance: 5,
            expires_at: None,
        },
        "manual",
        &json!([]),
    )
    .await
    .unwrap();
    let input = f.ingest(11, "10002", "30001", "你好，灵犀", true).await;
    let params = json!({"id":input["event_id"],"owner":false});
    worker::process(&f.app, &f.user, &params, &CancellationToken::new())
        .await
        .unwrap();
    worker::process(&f.app, &f.user, &params, &CancellationToken::new())
        .await
        .unwrap();
    assert_eq!(f.mock.sends.lock().unwrap().len(), 1);
    let requests = f.mock.model_requests.lock().unwrap();
    assert_eq!(requests.len(), 1);
    assert!(!requests[0].to_string().contains("PRIVATE_OWNER_SECRET"));
    assert!(!requests[0].to_string().contains("PRIVATE SYSTEM"));
    assert!(requests[0].get("tools").is_none());
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM messages")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(count, 0);
}
#[tokio::test]
async fn failed_or_uncertain_send_is_not_repeated() {
    let f = Fixture::new().await;
    *f.mock.fail_send.lock().unwrap() = true;
    let input = f.ingest(12, "10002", "30001", "请回复我", true).await;
    let params = json!({"id":input["event_id"],"owner":false});
    assert!(
        worker::process(&f.app, &f.user, &params, &CancellationToken::new())
            .await
            .is_err()
    );
    assert!(
        worker::process(&f.app, &f.user, &params, &CancellationToken::new())
            .await
            .is_err()
    );
    assert_eq!(f.mock.sends.lock().unwrap().len(), 1);
}
#[tokio::test]
async fn observations_require_real_evidence_and_manual_or_deleted_facts_are_preserved() {
    let f = Fixture::new().await;
    let first = f
        .ingest(20, "10002", "30001", "我喜欢先看结论", false)
        .await;
    let second = f
        .ingest(21, "10002", "30001", "请先直接说结论", false)
        .await;
    let sources = store::history(
        &f.app,
        &f.user,
        "30001",
        Some("10002"),
        false,
        db::now() + 1,
        10,
    )
    .await
    .unwrap();
    let valid = json!([{"content":"在这些交流中倾向先了解结论","kind":"observed_trait","confidence":0.8,"evidence":[{"event_id":first["event_id"],"quote":"喜欢先看结论"},{"event_id":second["event_id"],"quote":"先直接说结论"}]}]);
    worker::validate_observations(
        &serde_json::from_value::<Vec<worker::Observation>>(valid.clone()).unwrap(),
        &sources,
    )
    .unwrap();
    let mut invalid = valid.clone();
    invalid[0]["evidence"][1]["event_id"] = json!("made-up");
    assert!(
        worker::validate_observations(
            &serde_json::from_value::<Vec<worker::Observation>>(invalid).unwrap(),
            &sources
        )
        .is_err()
    );
    let mut invalid = valid;
    invalid[0]["evidence"].as_array_mut().unwrap().pop();
    assert!(
        worker::validate_observations(
            &serde_json::from_value::<Vec<worker::Observation>>(invalid).unwrap(),
            &sources
        )
        .is_err()
    );
    let id = f.fact("10002", "30001", "偏好先看结论").await;
    store::forget(&f.app, &f.user, &id).await.unwrap();
    store::save_fact(
        &f.app,
        &f.user,
        &store::FactInput {
            group_id: "30001".into(),
            qq_id: "10002".into(),
            name: "成员".into(),
            content: "偏好先看结论".into(),
            expires_at: None,
        },
        "auto",
        0.8,
        &json!([]),
    )
    .await
    .unwrap();
    assert!(
        store::fact(&f.app, &f.user, &id)
            .await
            .unwrap()
            .deleted_at
            .is_some()
    );
}
#[tokio::test]
async fn main_memory_extraction_skips_qq_owner_group_threads() {
    let f = Fixture::new().await;
    f.ingest(24, "10001", "30001", "张三喜欢喝咖啡，我只是转述", false)
        .await;
    let profile = settings::active_profile(&f.app, &f.user, None)
        .await
        .unwrap();
    let count = memory::extract(&f.app, &f.user, &profile, &CancellationToken::new())
        .await
        .unwrap();
    assert_eq!(count, 0);
    assert!(f.mock.model_requests.lock().unwrap().is_empty());
}
#[tokio::test]
async fn authenticated_member_details_do_not_cross_accounts_and_owner_binding_is_immutable() {
    let f = Fixture::new().await;
    let id = f.fact("10002", "30001", "喜欢详细说明").await;
    let other = auth::create_user(&f.app, "other", "test-other-password".into(), false)
        .await
        .unwrap();
    let token = auth::issue_token(&f.app, &other, "test", 3600)
        .await
        .unwrap();
    let response = lingxi::api::router(f.app.clone())
        .oneshot(
            Request::builder()
                .uri(format!("/api/v2/qq/memories/{id}"))
                .method("DELETE")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
    let _ = response.into_body().collect().await.unwrap();
    let mut cfg = integrations::config(&f.app, &f.user).await.unwrap();
    cfg["qq"]["owner_qq"] = json!("10002");
    assert!(
        integrations::save_config(&f.app, &f.user, cfg)
            .await
            .is_err()
    );
    for empty in [
        json!({}),
        json!({"enabled":false,"owner_qq":""}),
        Value::Null,
    ] {
        assert!(
            integrations::save_config(&f.app, &f.user, json!({"qq":empty}))
                .await
                .is_err()
        );
    }
    assert_eq!(qq::config(&f.app, &f.user).await.unwrap().owner_qq, "10001");
}

#[test]
fn qq_identifiers_have_one_canonical_representation() {
    for invalid in [
        "",
        "0",
        "0010001",
        "-10001",
        "18446744073709551616",
        "１０００１",
    ] {
        assert!(!qq::valid_id(invalid), "{invalid}");
        assert!(qq::numeric_id(&json!(invalid)).is_none());
    }
    assert_eq!(
        qq::numeric_id(&json!(10001)),
        qq::numeric_id(&json!("10001"))
    );
}

#[tokio::test]
async fn owner_reply_is_saved_once_in_main_conversation_only() {
    let f = Fixture::new().await;
    let event = f
        .ingest(100, "10001", "30001", "我想整理今天的计划", true)
        .await;
    let params = json!({"id":event["event_id"],"owner":true});
    for _ in 0..2 {
        worker::process(&f.app, &f.user, &params, &CancellationToken::new())
            .await
            .unwrap();
    }
    let messages: Vec<(String, String)> =
        sqlx::query_as("SELECT role,content FROM messages ORDER BY id")
            .fetch_all(&f.app.pool)
            .await
            .unwrap();
    assert_eq!(
        messages,
        vec![
            ("user".into(), "我想整理今天的计划".into()),
            ("assistant".into(), "我在，欢迎聊聊今天的计划。".into())
        ]
    );
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM events")
        .fetch_one(store::pool(&f.app).await.unwrap())
        .await
        .unwrap();
    assert_eq!(count, 0);
    assert_eq!(f.mock.sends.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn extracted_traits_follow_speaker_ownership_with_validated_evidence() {
    let f = Fixture::new().await;
    let mut cfg = integrations::config(&f.app, &f.user).await.unwrap();
    cfg["qq"]["collect_memory"] = json!(true);
    integrations::save_config(&f.app, &f.user, cfg)
        .await
        .unwrap();
    for (qq, text, storage, base) in [
        ("10001", "我喜欢安静思考", "main", 200),
        ("10002", "我喜欢热闹讨论", "qq", 300),
    ] {
        let first = f.ingest(base, qq, "30001", text, false).await;
        let second = f.ingest(base + 1, qq, "30001", text, false).await;
        *f.mock.model_response.lock().unwrap() = Some(json!([{"content":text,"kind":"observed_trait","confidence":0.8,"evidence":[{"event_id":first["event_id"],"quote":text},{"event_id":second["event_id"],"quote":text}]}]).to_string());
        let result = worker::extract(
            &f.app,
            &f.user,
            &json!({"group_id":"30001","qq_id":qq}),
            &CancellationToken::new(),
        )
        .await
        .unwrap();
        assert_eq!(result["storage"], storage);
        assert_eq!(result["saved"], 1);
    }
    let memories = memory::list(&f.app, &f.user).await.unwrap();
    assert_eq!(memories.len(), 1);
    assert_eq!(memories[0].content, "我喜欢安静思考");
    assert_eq!(memories[0].expires_at, None);
    assert_eq!(memories[0].provenance["qq_id"], "10001");
    let detail = qq::api::person(
        State(f.app.clone()),
        f.user.clone(),
        axum::extract::Query(qq::api::PersonQuery {
            group_id: "30001".into(),
            qq_id: "10001".into(),
            q: None,
        }),
    )
    .await
    .unwrap()
    .0;
    assert_eq!(detail["data"]["storage"], "main");
    assert_eq!(
        detail["data"]["memories"][0]["evidence"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    assert_eq!(detail["data"]["memories"][0]["confidence"], 0.8);
    let facts = store::facts(&f.app, &f.user, "30001", "10002")
        .await
        .unwrap();
    assert_eq!(facts.len(), 1);
    assert_eq!(facts[0].content, "我喜欢热闹讨论");
    assert!(
        store::facts(&f.app, &f.user, "30001", "10001")
            .await
            .unwrap()
            .is_empty()
    );
    let requests = f.mock.model_requests.lock().unwrap();
    assert!(!requests[0].to_string().contains("热闹讨论"));
    assert!(!requests[1].to_string().contains("安静思考"));
}

#[tokio::test]
#[ignore = "requires isolated Qdrant; set LINGXI_TEST_QDRANT_URL and run --ignored"]
async fn real_qdrant_indexes_filters_and_deletes_member_vectors() {
    let f = Fixture::new().await;
    let url = std::env::var("LINGXI_TEST_QDRANT_URL").expect("isolated Qdrant URL required");
    let mut config = integrations::config(&f.app, &f.user).await.unwrap();
    config["qq"]["vector"]["url"] = json!(url);
    integrations::save_config(&f.app, &f.user, config)
        .await
        .unwrap();
    let own = f.fact("10002", "30001", "偏好先说结论").await;
    let other = f.fact("10003", "30001", "喜欢详细解释").await;
    let other_group = f.fact("10002", "30002", "喜欢慢慢讨论").await;
    for id in [&own, &other, &other_group] {
        vector::index(&f.app, &f.user, id).await.unwrap();
    }
    let rows = vector::recall(&f.app, &f.user, "30001", "10002", "交流偏好")
        .await
        .unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].id, own);
    assert_eq!(rows[0].revision, rows[0].indexed_revision);
    store::forget(&f.app, &f.user, &own).await.unwrap();
    vector::index(&f.app, &f.user, &own).await.unwrap();
    assert!(
        vector::recall(&f.app, &f.user, "30001", "10002", "交流偏好")
            .await
            .unwrap()
            .is_empty()
    );
    let cfg = qq::config(&f.app, &f.user).await.unwrap();
    let collection = vector::collection(&f.user, &cfg);
    let count: Value = reqwest::Client::new()
        .get(format!("{url}/collections/{collection}"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(count["result"]["points_count"], 2);
    reqwest::Client::new()
        .delete(format!("{url}/collections/{collection}"))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap();
}
