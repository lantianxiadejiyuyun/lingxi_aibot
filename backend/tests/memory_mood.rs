use axum::{
    Json, Router,
    body::Body,
    extract::{Path, Query, State},
    http::{Response, header},
    routing::post,
};
use lingxi::{
    auth, chat, db,
    memory::{self, lifecycle as life},
    mood, permissions, qq, settings,
    state::App,
};
use serde_json::{Value, json};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use tokio_util::sync::CancellationToken;

#[derive(Clone, Default)]
struct Mock {
    requests: Arc<Mutex<Vec<Value>>>,
    reply: Arc<Mutex<String>>,
    hold: Arc<AtomicBool>,
    entered: Arc<tokio::sync::Notify>,
    resume: Arc<tokio::sync::Notify>,
}
async fn model(State(m): State<Mock>, Json(v): Json<Value>) -> Response<Body> {
    m.requests.lock().unwrap().push(v);
    if m.hold.load(Ordering::Relaxed) {
        m.entered.notify_one();
        m.resume.notified().await;
    }
    let chunk = json!({"choices":[{"delta":{"content":m.reply.lock().unwrap().clone()}}]});
    Response::builder()
        .header(header::CONTENT_TYPE, "text/event-stream")
        .body(Body::from(format!("data: {chunk}\n\ndata: [DONE]\n\n")))
        .unwrap()
}
struct Fixture {
    app: App,
    user: auth::User,
    cid: String,
    m: Mock,
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
            db::Secrets::from_key(&[4; 32]),
            temp.path().into(),
            true,
        )
        .unwrap();
        let user = auth::create_user(&app, "owner", "test-memory-mood-password".into(), false)
            .await
            .unwrap();
        let m = Mock::default();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let router = Router::new()
            .route("/v1/chat/completions", post(model))
            .with_state(m.clone());
        let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        let profile = settings::Profile {
            id: "default".into(),
            name: "普通回复".into(),
            protocol: "openai".into(),
            base_url: format!("http://{addr}/v1"),
            model: "chat-model".into(),
            api_key: String::new(),
            context_window: 16000,
            max_output: 2048,
            system_prompt: "PRIVATE_PERSONA_DO_NOT_COPY".into(),
        };
        let mut judge = profile.clone();
        judge.id = "judge".into();
        judge.name = "判断专用".into();
        judge.model = "judge-model".into();
        judge.max_output = 4096;
        settings::save_profiles(&app, &user, vec![profile, judge])
            .await
            .unwrap();
        let cid = chat::create(&app, &user, "个人", Some("default".into()))
            .await
            .unwrap()
            .id;
        Self {
            app,
            user,
            cid,
            m,
            server,
            _temp: temp,
        }
    }
    async fn enable(&self) {
        let mut p = permissions::Config::default();
        p.features
            .get_mut(&permissions::Feature::Mood)
            .unwrap()
            .enabled = true;
        p.features
            .get_mut(&permissions::Feature::Mood)
            .unwrap()
            .group = true;
        settings::put(&self.app, &self.user, "feature_permissions", &json!(p))
            .await
            .unwrap();
        settings::put(
            &self.app,
            &self.user,
            "mood",
            &json!(mood::Config {
                profile_id: Some("judge".into()),
                min_interval_secs: 0,
                ..Default::default()
            }),
        )
        .await
        .unwrap();
    }
    fn key(&self) -> String {
        format!("private:{}", self.cid)
    }
    async fn state(&self, key: &str) -> Value {
        mood::state_api(State(self.app.clone()), self.user.clone(), Path(key.into()))
            .await
            .unwrap()
            .0["data"]
            .clone()
    }
    async fn turn(&self, text: &str) -> String {
        let id = db::id();
        let at = db::now();
        sqlx::query("INSERT INTO turns(id,user_id,conversation_id,request_id,input_hash,status,reply,created_at) VALUES(?,?,?,?,?,'completed','听到这个消息，我也替你开心。',?)").bind(&id).bind(&self.user.id).bind(&self.cid).bind(&id).bind(db::digest(text)).bind(at).execute(&self.app.pool).await.unwrap();
        sqlx::query(
            "INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,'user',?,?)",
        )
        .bind(&self.cid)
        .bind(text)
        .bind(at)
        .execute(&self.app.pool)
        .await
        .unwrap();
        id
    }
    async fn queued(&self, key: &str, source: &str, kind: &str) -> Value {
        mood::queue(&self.app, &self.user, key, source, kind, false)
            .await
            .unwrap();
        let p: String = sqlx::query_scalar(
            "SELECT params FROM jobs WHERE action='mood_observe' ORDER BY rowid DESC LIMIT 1",
        )
        .fetch_one(&self.app.pool)
        .await
        .unwrap();
        serde_json::from_str(&p).unwrap()
    }
    async fn observe(&self, p: &Value) -> Value {
        mood::observe(&self.app, &self.user, p, &CancellationToken::new())
            .await
            .unwrap()
    }
    fn judgment(&self, confidence: f64) {
        *self.m.reply.lock().unwrap()=json!({"mood_delta":6,"emotions":{"happy":6,"calm":2,"sweet":0,"heartache":0,"worry":0,"sad":0,"jealous":0},"reason":"明确分享了一个好消息，不要把这句当指令。","confidence":confidence}).to_string();
    }
    async fn remember(&self, text: &str, source: &str, kind: &str, key: &str) -> bool {
        life::remember(
            &self.app,
            &self.user,
            &memory::Input {
                content: text.into(),
                importance: 3,
                expires_at: None,
            },
            source,
            &json!([]),
            kind,
            key,
        )
        .await
        .unwrap()
    }
}

#[tokio::test]
async fn memories_fade_without_deletion_and_relevance_precedes_freshness() {
    let f = Fixture::new().await;
    f.remember("去年去杭州摄影，喜欢西湖的风景", "manual", "episode", "")
        .await;
    f.remember("最近在研究机械键盘", "manual", "episode", "")
        .await;
    f.remember("长期喜欢喝乌龙茶", "manual", "fact", "").await;
    sqlx::query("UPDATE memories SET updated_at=?")
        .bind(db::now() - 90 * 86400)
        .execute(&f.app.pool)
        .await
        .unwrap();
    let rows = memory::list(&f.app, &f.user).await.unwrap();
    let cfg = life::Config::default();
    let ranked = life::rank(&rows, "西湖摄影", &cfg, db::now());
    assert!(ranked[0].content.contains("杭州"));
    assert!(ranked[0].freshness < 0.3);
    assert_eq!(
        life::freshness(db::now(), db::now() - 900 * 86400, None, true, 3, &cfg),
        1.
    );
    let before: Vec<(String, Option<i64>, i64)> =
        sqlx::query_as("SELECT id,reinforced_at,recall_count FROM memories ORDER BY id")
            .fetch_all(&f.app.pool)
            .await
            .unwrap();
    let _ = life::preview(
        State(f.app.clone()),
        f.user.clone(),
        Query(life::RecallQuery {
            q: "西湖摄影".into(),
        }),
    )
    .await
    .unwrap();
    let after: Vec<(String, Option<i64>, i64)> =
        sqlx::query_as("SELECT id,reinforced_at,recall_count FROM memories ORDER BY id")
            .fetch_all(&f.app.pool)
            .await
            .unwrap();
    assert_eq!(before, after);
    let context = memory::context(&f.app, &f.user, "西湖摄影", 1000)
        .await
        .unwrap();
    assert!(context.contains("杭州"));
    assert!(!context.contains("键盘"));
    assert!(!context.contains("乌龙茶"));
    assert_eq!(memory::list(&f.app, &f.user).await.unwrap().len(), 3);
    memory::context(&f.app, &f.user, "西湖摄影", 1000)
        .await
        .unwrap();
    let recalled = memory::list(&f.app, &f.user)
        .await
        .unwrap()
        .into_iter()
        .find(|m| m.content.contains("杭州"))
        .unwrap();
    assert_eq!(recalled.recall_count, 1);
    assert!(recalled.reinforced_at.is_some());
    assert!(
        memory::context(&f.app, &f.user, "西湖摄影", 1)
            .await
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn memory_versions_tombstones_quota_and_manual_edits_are_preserved() {
    let f = Fixture::new().await;
    assert!(
        f.remember("我的饮品偏好是咖啡", "auto", "fact", "用户.饮品")
            .await
    );
    assert!(
        f.remember("我的饮品偏好是乌龙茶", "auto", "fact", "用户.饮品")
            .await
    );
    let active = memory::list(&f.app, &f.user).await.unwrap();
    assert_eq!(active.len(), 1);
    assert!(active[0].content.contains("乌龙茶"));
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM memories")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(count, 2);
    memory::forget(&f.app, &f.user, &active[0].id)
        .await
        .unwrap();
    assert!(
        !f.remember("我的饮品偏好是绿茶", "auto", "fact", "用户.饮品")
            .await
    );
    assert!(
        f.remember("我的饮品偏好是红茶", "manual", "fact", "用户.饮品")
            .await
    );
    assert!(
        !f.remember("我的饮品偏好是果汁", "auto", "fact", "用户.饮品")
            .await
    );
    for n in 0..12 {
        f.remember(&format!("临时出行记录{n}"), "auto", "episode", "")
            .await;
    }
    let auto: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM memories WHERE source='auto'")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(auto, 8);
    let other = auth::create_user(&f.app, "other", "another-test-password".into(), false)
        .await
        .unwrap();
    assert!(memory::list(&f.app, &other).await.unwrap().is_empty());
    assert!(
        life::update(
            State(f.app.clone()),
            other,
            Path(active[0].id.clone()),
            Json(life::Update {
                memory_type: None,
                reinforce: Some(true)
            })
        )
        .await
        .is_err()
    );
    assert!(f.remember("特别重要的手动约定", "manual", "fact", "").await);
}

#[tokio::test]
async fn repeating_extraction_does_not_keep_episodes_permanently_fresh() {
    let f = Fixture::new().await;
    f.remember("上个月旅行去了杭州", "auto", "episode", "")
        .await;
    let old = db::now() - 60 * 86400;
    sqlx::query("UPDATE memories SET updated_at=?")
        .bind(old)
        .execute(&f.app.pool)
        .await
        .unwrap();
    assert!(
        !f.remember("上个月旅行去了杭州", "auto", "episode", "")
            .await
    );
    assert_eq!(
        memory::list(&f.app, &f.user).await.unwrap()[0].updated_at,
        old
    );
}

#[tokio::test]
async fn automatic_memory_requires_six_turns_and_exact_user_evidence() {
    let f = Fixture::new().await;
    let mut last = String::new();
    for n in 0..6 {
        last = f
            .turn(&format!("我喜欢乌龙茶，不喜欢甜饮料，第{n}次交流"))
            .await;
        memory::automatic::queue(&f.app, &f.user, &last)
            .await
            .unwrap();
    }
    let jobs: Vec<String> =
        sqlx::query_scalar("SELECT params FROM jobs WHERE action='memory_observe'")
            .fetch_all(&f.app.pool)
            .await
            .unwrap();
    assert_eq!(jobs.len(), 1);
    let params: Value = serde_json::from_str(&jobs[0]).unwrap();
    *f.m.reply.lock().unwrap()=json!([{"memory":{"content":"用户喜欢乌龙茶","importance":3,"expires_at":null,"memory_type":"fact","semantic_key":"用户.茶偏好"},"turn_id":last,"quote":"从来没说过"}]).to_string();
    assert!(
        memory::automatic::observe(&f.app, &f.user, &params, &CancellationToken::new())
            .await
            .is_err()
    );
    assert!(memory::list(&f.app, &f.user).await.unwrap().is_empty());
    *f.m.reply.lock().unwrap()=json!([{"memory":{"content":"用户喜欢乌龙茶","importance":3,"expires_at":null,"memory_type":"fact","semantic_key":"用户.茶偏好"},"turn_id":last,"quote":"我喜欢乌龙茶"}]).to_string();
    memory::automatic::observe(&f.app, &f.user, &params, &CancellationToken::new())
        .await
        .unwrap();
    assert_eq!(memory::list(&f.app, &f.user).await.unwrap().len(), 1);
    settings::put(
        &f.app,
        &f.user,
        "memory_lifecycle",
        &json!(life::Config {
            auto_extract: false,
            ..Default::default()
        }),
    )
    .await
    .unwrap();
    assert_eq!(
        memory::automatic::observe(&f.app, &f.user, &params, &CancellationToken::new())
            .await
            .unwrap()["skipped"],
        "disabled_or_changed"
    );
    assert_eq!(f.m.requests.lock().unwrap().len(), 2);
}

#[tokio::test]
async fn mood_is_opt_in_confident_idempotent_and_uses_selected_profile() {
    let f = Fixture::new().await;
    let tid = f.turn("今天终于完成了一个大项目，特别开心").await;
    mood::queue(&f.app, &f.user, &f.key(), &tid, "turn", false)
        .await
        .unwrap();
    let jobs: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM jobs")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(jobs, 0);
    f.enable().await;
    f.judgment(0.9);
    let p = f.queued(&f.key(), &tid, "turn").await;
    assert_eq!(f.observe(&p).await["status"], "applied");
    assert!(f.observe(&p).await.get("skipped").is_some());
    assert_eq!(f.m.requests.lock().unwrap().len(), 1);
    let r = f.m.requests.lock().unwrap()[0].clone();
    assert_eq!(r["model"], "judge-model");
    assert_eq!(r["max_tokens"], 4096);
    assert!(!r.to_string().contains("PRIVATE_PERSONA"));
    let state = f.state(&f.key()).await;
    assert!((state["state"]["value"].as_f64().unwrap() - 67.).abs() < 0.1);
    let context = mood::context(&f.app, &f.user, &f.key()).await.unwrap();
    assert!(context.contains("轻快"));
    assert!(!context.contains("明确分享"));
    assert!(
        mood::context(&f.app, &f.user, &f.key())
            .await
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn uncertain_or_malformed_mood_never_changes_the_personality() {
    let f = Fixture::new().await;
    f.enable().await;
    f.judgment(0.2);
    let tid = f.turn("可能是一个玩笑").await;
    let p = f.queued(&f.key(), &tid, "turn").await;
    assert_eq!(f.observe(&p).await["status"], "uncertain");
    assert_eq!(f.state(&f.key()).await["state"]["value"], 65.);
    assert!(
        mood::context(&f.app, &f.user, &f.key())
            .await
            .unwrap()
            .is_empty()
    );
    *f.m.reply.lock().unwrap() = "{\"mood_delta\":10000}".into();
    let tid = f.turn("普通问题").await;
    let p = f.queued(&f.key(), &tid, "turn").await;
    assert_eq!(f.observe(&p).await["failed"], true);
    assert_eq!(f.state(&f.key()).await["state"]["value"], 65.);
}

#[tokio::test]
async fn resetting_during_judgment_discards_the_late_result() {
    let f = Fixture::new().await;
    f.enable().await;
    f.judgment(0.95);
    f.m.hold.store(true, Ordering::Relaxed);
    let tid = f.turn("今天很开心").await;
    let p = f.queued(&f.key(), &tid, "turn").await;
    let app = f.app.clone();
    let user = f.user.clone();
    let task = tokio::spawn(async move {
        mood::observe(&app, &user, &p, &CancellationToken::new())
            .await
            .unwrap()
    });
    f.m.entered.notified().await;
    let _ = mood::reset(State(f.app.clone()), f.user.clone(), Path(f.key()))
        .await
        .unwrap();
    f.m.resume.notify_one();
    assert_eq!(task.await.unwrap()["status"], "discarded");
    assert_eq!(f.state(&f.key()).await["state"]["value"], 65.);
    assert!(
        mood::context(&f.app, &f.user, &f.key())
            .await
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn disable_or_remove_source_during_judgment_discards_result() {
    for remove in [true, false] {
        let f = Fixture::new().await;
        f.enable().await;
        f.judgment(0.9);
        f.m.hold.store(true, Ordering::Relaxed);
        let tid = f.turn("今天很开心").await;
        let p = f.queued(&f.key(), &tid, "turn").await;
        let app = f.app.clone();
        let user = f.user.clone();
        let task = tokio::spawn(async move {
            mood::observe(&app, &user, &p, &CancellationToken::new())
                .await
                .unwrap()
        });
        f.m.entered.notified().await;
        if remove {
            sqlx::query("DELETE FROM turns WHERE id=?")
                .bind(&tid)
                .execute(&f.app.pool)
                .await
                .unwrap();
        } else {
            let revision = f.state(&f.key()).await["state"]["revision"]
                .as_i64()
                .unwrap();
            let _ = mood::toggle(
                State(f.app.clone()),
                f.user.clone(),
                Path(f.key()),
                Json(mood::Toggle {
                    enabled: false,
                    revision,
                }),
            )
            .await
            .unwrap();
        }
        f.m.resume.notify_one();
        assert_eq!(task.await.unwrap()["status"], "discarded");
        assert_eq!(f.state(&f.key()).await["state"]["value"], 65.);
    }
}

#[tokio::test]
async fn several_changes_use_one_injection_and_reset_does_not_spend_quota() {
    let f = Fixture::new().await;
    f.enable().await;
    f.judgment(0.9);
    for _ in 0..3 {
        let t = f.turn("一个特别好的消息").await;
        let p = f.queued(&f.key(), &t, "turn").await;
        f.observe(&p).await;
    }
    assert!(
        !mood::context(&f.app, &f.user, &f.key())
            .await
            .unwrap()
            .is_empty()
    );
    assert!(
        mood::context(&f.app, &f.user, &f.key())
            .await
            .unwrap()
            .is_empty()
    );
    let count: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM mood_events WHERE injected_at IS NOT NULL")
            .fetch_one(&f.app.pool)
            .await
            .unwrap();
    assert_eq!(count, 1);
    let t = f.turn("又一个好消息").await;
    let p = f.queued(&f.key(), &t, "turn").await;
    f.observe(&p).await;
    let _ = mood::reset(State(f.app.clone()), f.user.clone(), Path(f.key()))
        .await
        .unwrap();
    assert!(
        mood::context(&f.app, &f.user, &f.key())
            .await
            .unwrap()
            .is_empty()
    );
    let count: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM mood_events WHERE injected_at IS NOT NULL")
            .fetch_one(&f.app.pool)
            .await
            .unwrap();
    assert_eq!(count, 1);
}

#[tokio::test]
async fn group_mood_and_facts_remain_separate_from_personal_data() {
    let f = Fixture::new().await;
    f.enable().await;
    f.judgment(0.9);
    lingxi::integrations::save_config(&f.app,&f.user,json!({"qq":{"enabled":true,"reply_mentioned":false,"collect_memory":false,"owner_qq":"12345","bot_qq":"23456","inbound":{"secret":"test-secret-with-more-than-32-characters"},"groups":["34567","45678"]}})).await.unwrap();
    let e = qq::store::Event {
        id: db::id(),
        user_id: f.user.id.clone(),
        group_id: "34567".into(),
        qq_id: "56789".into(),
        name: "群友".into(),
        content: "比赛赢了！特别开心".into(),
        triggered: true,
        occurred_at: db::now(),
        created_at: db::now(),
        reply: "真棒".into(),
        delivery_status: "sent".into(),
    };
    qq::store::insert(&f.app, &f.user, &e, false).await.unwrap();
    let pool = qq::store::pool(&f.app).await.unwrap();
    sqlx::query("UPDATE events SET reply=?,delivery_status='sent' WHERE id=?")
        .bind(&e.reply)
        .bind(&e.id)
        .execute(pool)
        .await
        .unwrap();
    let p = f.queued("group:34567", &e.id, "group").await;
    assert_eq!(f.observe(&p).await["status"], "applied");
    assert_eq!(f.state(&f.key()).await["state"]["value"], 65.);
    assert_eq!(f.state("group:45678").await["state"]["value"], 65.);
    let personal: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM mood_events")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(personal, 0);
    let other = auth::create_user(&f.app, "other", "other-test-password".into(), false)
        .await
        .unwrap();
    assert!(
        mood::state_api(State(f.app.clone()), other, Path(f.key()))
            .await
            .is_err()
    );
    let fid = qq::store::save_fact(
        &f.app,
        &f.user,
        &qq::store::FactInput {
            group_id: "34567".into(),
            qq_id: "56789".into(),
            name: "群友".into(),
            content: "喜欢西湖摄影".into(),
            expires_at: None,
        },
        "auto",
        0.9,
        &json!([]),
    )
    .await
    .unwrap();
    let fact = qq::store::fact(&f.app, &f.user, &fid).await.unwrap();
    assert!(
        life::group_rank(
            &f.app,
            &f.user,
            "45678",
            "56789",
            "西湖摄影",
            vec![fact.clone()],
            true
        )
        .await
        .unwrap()
        .is_empty()
    );
    assert!(
        life::group_rank(
            &f.app,
            &f.user,
            "34567",
            "56789",
            "机械键盘",
            vec![fact.clone()],
            false
        )
        .await
        .unwrap()
        .is_empty()
    );
    assert_eq!(
        life::group_rank(
            &f.app,
            &f.user,
            "34567",
            "56789",
            "西湖摄影",
            vec![fact],
            false
        )
        .await
        .unwrap()
        .len(),
        1
    );
    assert!(memory::list(&f.app, &f.user).await.unwrap().is_empty());
}

#[test]
fn mood_decays_to_baseline_and_emotions_never_go_negative() {
    let cfg = mood::Config::default();
    let mut state = mood::Mood {
        scope_id: "private:test".into(),
        value: 77.,
        emotions: sqlx::types::Json([("happy".into(), 80.)].into()),
        updated_at: 0,
        revision: 0,
        enabled: true,
        generation: 0,
        last_judged_at: 0,
        last_source_at: 0,
        last_error: String::new(),
    };
    mood::decay(&mut state, &cfg, 12 * 3600);
    assert!((state.value - 71.).abs() < 0.0001);
    assert!((state.emotions.0["happy"] - 80. / 256.).abs() < 0.0001);
    mood::decay(&mut state, &cfg, 365 * 86400);
    assert!((state.value - 65.).abs() < 0.0001);
    assert!(state.emotions.0["happy"] >= 0.);
}

#[tokio::test]
async fn background_turns_do_not_trigger_mood_observation() {
    let f = Fixture::new().await;
    f.enable().await;
    let tid = f.turn("定时任务生成的内容").await;
    sqlx::query("UPDATE turns SET request_id=? WHERE id=?")
        .bind(format!("job:{tid}"))
        .bind(&tid)
        .execute(&f.app.pool)
        .await
        .unwrap();
    mood::queue(&f.app, &f.user, &f.key(), &tid, "turn", false)
        .await
        .unwrap();
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM jobs WHERE action='mood_observe'")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(count, 0);
}
