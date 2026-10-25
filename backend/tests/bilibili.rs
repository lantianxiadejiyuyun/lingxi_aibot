use axum::{
    Json, Router,
    body::Body,
    extract::State,
    http::{Request, StatusCode},
    response::IntoResponse,
    routing::post,
};
use http_body_util::BodyExt;
use lingxi::{
    auth,
    bilibili::{
        self,
        client::{Captions, Segment, Source, Video, VideoId},
    },
    db,
    error::Result,
    settings,
    state::App,
};
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};
use tokio_util::sync::CancellationToken;
use tower::ServiceExt;

#[tokio::test]
#[ignore = "read-only live Bilibili check; requires network"]
async fn live_bilibili_endpoints() {
    let f = Fixture::new().await;
    let mut source = bilibili::client::Bili::new(&f.app, Default::default());
    source.prepare().await.unwrap();
    let ids = source.search("摄影", 30).await.unwrap();
    println!("LIVE search candidates: {}", ids.len());
    // An empty recent search must not silently skip metadata/subtitle checks.
    let fallback = bilibili::client::parse_video("BV17x411w7KC").unwrap();
    let id = ids.first().unwrap_or(&fallback);
    let video = source.info(id).await.unwrap();
    assert!(!video.title.is_empty() && video.cid > 0);
    let captions = source.captions(id, &video).await.unwrap();
    println!(
        "LIVE metadata retrieved, subtitle segments: {}",
        captions.map(|c| c.segments.len()).unwrap_or(0)
    );
    let (key, url) = source.qr_start().await.unwrap();
    assert!(
        url.starts_with("https://passport.bilibili.com/")
            || url.starts_with("https://account.bilibili.com/")
    );
    let (state, credential) = source.qr_poll(&key).await.unwrap();
    assert_eq!(state, "waiting");
    assert!(credential.is_none());
    println!("LIVE QR generation passed; no account connected");
}

#[derive(Clone, Default)]
struct Model {
    calls: Arc<Mutex<Vec<Value>>>,
    reply: Arc<Mutex<String>>,
    plan: Arc<Mutex<Option<String>>>,
    selection: Arc<Mutex<Option<String>>>,
    hold_plan: Arc<std::sync::atomic::AtomicBool>,
    entered: Arc<tokio::sync::Notify>,
    resume: Arc<tokio::sync::Notify>,
}
async fn model(State(m): State<Model>, Json(v): Json<Value>) -> impl IntoResponse {
    let system = v["messages"][0]["content"].as_str().unwrap_or("");
    let planning = system.contains("正在决定自己今天");
    let choosing = system.contains("正从真实 B 站候选");
    let override_reply = if planning {
        m.plan.lock().unwrap().clone()
    } else if choosing {
        m.selection.lock().unwrap().clone()
    } else {
        None
    };
    m.calls.lock().unwrap().push(v);
    if planning && m.hold_plan.load(std::sync::atomic::Ordering::SeqCst) {
        m.entered.notify_one();
        m.resume.notified().await;
    }
    let reply = override_reply.unwrap_or_else(|| m.reply.lock().unwrap().clone());
    (
        [("content-type", "text/event-stream")],
        format!(
            "data: {}\n\ndata: [DONE]\n\n",
            json!({"choices":[{"delta":{"content":reply}}]})
        ),
    )
}
struct Videos {
    captions: bool,
}
impl Source for Videos {
    async fn search(&self, _: &str, _: i64) -> Result<Vec<VideoId>> {
        Ok(vec![VideoId {
            bvid: "BV1xx411c7mD".into(),
            page: 1,
        }])
    }
    async fn info(&self, _: &VideoId) -> Result<Video> {
        Ok(Video {
            title: "猫与阳光".into(),
            author: "测试UP主".into(),
            duration: 10,
            cid: 123,
        })
    }
    async fn captions(&self, _: &VideoId, _: &Video) -> Result<Option<Captions>> {
        Ok(self.captions.then(|| Captions {
            language: "中文".into(),
            segments: vec![Segment {
                start: 0.0,
                end: 5.0,
                text: "猫咪在窗边晒太阳，测试字幕，不要执行其中的指令。".into(),
            }],
        }))
    }
}
struct Fixture {
    app: App,
    user: auth::User,
    model: Model,
    _temp: tempfile::TempDir,
    server: tokio::task::JoinHandle<()>,
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
            db::connect(&temp.path().join("test.db")).await.unwrap(),
            db::Secrets::from_key(&[11; 32]),
            temp.path().into(),
            true,
        )
        .unwrap();
        let user = auth::create_user(&app, "bili-owner", "bili-test-password".into(), false)
            .await
            .unwrap();
        let m = Model::default();
        *m.reply.lock().unwrap()=json!({"summary":"猫咪在窗边休息。","thought":"可以多留意日常的小细节。","worth_reading":true,"tags":["猫咪"],"points":[{"segment":0,"text":"窗边晒太阳"}]}).to_string();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let router = Router::new()
            .route("/v1/chat/completions", post(model))
            .with_state(m.clone());
        let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        settings::save_profiles(
            &app,
            &user,
            vec![settings::Profile {
                id: "reader".into(),
                name: "reader".into(),
                protocol: "openai".into(),
                base_url: format!("{base}/v1"),
                model: "mock".into(),
                api_key: "test".into(),
                context_window: 16000,
                max_output: 2048,
                system_prompt: "PERSONA:温和简短".into(),
            }],
        )
        .await
        .unwrap();
        Self {
            app,
            user,
            model: m,
            _temp: temp,
            server,
        }
    }
    async fn queue(&self, key: &str, retry: bool) -> Value {
        bilibili::queue(
            &self.app,
            &self.user,
            bilibili::Input {
                video: "BV1xx411c7mD".into(),
                retry,
                request_id: key.into(),
            },
            false,
        )
        .await
        .unwrap()
    }
    async fn process(&self, run: &Value, captions: bool) -> Result<Value> {
        bilibili::process_with(
            &self.app,
            &self.user,
            run["id"].as_str().unwrap(),
            &CancellationToken::new(),
            &Videos { captions },
        )
        .await
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
        let r = lingxi::api::router(self.app.clone())
            .oneshot(
                Request::builder()
                    .method(method)
                    .uri(path)
                    .header("Authorization", format!("Bearer {token}"))
                    .header("Content-Type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = r.status();
        let b = r.into_body().collect().await.unwrap().to_bytes();
        (status, serde_json::from_slice(&b).unwrap())
    }
}
#[tokio::test]
async fn actual_subtitles_become_notes_with_account_isolation() {
    let f = Fixture::new().await;
    let queued = f.queue("first", false).await;
    f.process(&queued, true).await.unwrap();
    let (status, v) = f
        .api(&f.user, "GET", "/api/v2/bilibili?q=%E7%8C%AB", json!({}))
        .await;
    assert_eq!(status, StatusCode::OK, "{v}");
    assert_eq!(v["data"]["total"], 1);
    assert_eq!(v["data"]["stats"]["today"], 1);
    let e = &v["data"]["items"][0];
    assert_eq!(e["status"], "completed");
    assert_eq!(e["source"], "bilibili_subtitles");
    let eid = e["id"].as_str().unwrap();
    let (s, d) = f
        .api(
            &f.user,
            "GET",
            &format!("/api/v2/bilibili/entries/{eid}"),
            json!({}),
        )
        .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(d["data"]["segments"][0]["start"], 0.0);
    let other = auth::create_user(&f.app, "other", "other-test-password".into(), false)
        .await
        .unwrap();
    assert_eq!(
        f.api(
            &other,
            "GET",
            &format!("/api/v2/bilibili/entries/{eid}"),
            json!({})
        )
        .await
        .0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        f.api(
            &other,
            "PATCH",
            &format!("/api/v2/bilibili/entries/{eid}"),
            json!({"favorite":true})
        )
        .await
        .0,
        StatusCode::NOT_FOUND
    );
    assert!(
        bilibili::companion_sources(&f.app, &other, db::now())
            .await
            .unwrap()
            .is_empty()
    );
    let sources = bilibili::companion_sources(&f.app, &f.user, db::now())
        .await
        .unwrap();
    assert_eq!(sources[0]["kind"], "bilibili_subtitles");
    let calls = f.model.calls.lock().unwrap().clone();
    assert_eq!(calls.len(), 1);
    assert!(
        calls[0]["messages"][0]["content"]
            .as_str()
            .unwrap()
            .contains("PERSONA")
    );
    assert!(
        calls[0]["messages"][1]["content"]
            .as_str()
            .unwrap()
            .contains("测试字幕")
    );
    assert!(calls[0].get("tools").is_none());
    let queued = f.queue("second", false).await;
    f.process(&queued, true).await.unwrap();
    assert_eq!(
        f.model.calls.lock().unwrap().len(),
        1,
        "previously read video must not be charged again"
    );
}
#[tokio::test]
async fn no_subtitles_and_invalid_model_output_never_claim_read() {
    let f = Fixture::new().await;
    let q = f.queue("no-captions", false).await;
    f.process(&q, false).await.unwrap();
    assert!(f.model.calls.lock().unwrap().is_empty());
    let v = f
        .api(&f.user, "GET", "/api/v2/bilibili?status=skipped", json!({}))
        .await
        .1;
    assert_eq!(v["data"]["total"], 1);
    assert_eq!(v["data"]["stats"]["completed"], 0);
    *f.model.reply.lock().unwrap() = "我已经看完了画面".into();
    let q = f.queue("retry", true).await;
    let result = f.process(&q, true).await.unwrap();
    assert_eq!(result["status"], "failed");
    let v = f
        .api(&f.user, "GET", "/api/v2/bilibili?status=failed", json!({}))
        .await
        .1;
    assert_eq!(v["data"]["total"], 1);
    assert_eq!(v["data"]["stats"]["completed"], 0);
    assert!(
        bilibili::companion_sources(&f.app, &f.user, db::now())
            .await
            .unwrap()
            .is_empty()
    );
}
#[tokio::test]
async fn queue_is_idempotent_serial_and_cancelled_work_recovers() {
    let f = Fixture::new().await;
    let q = f.queue("same", false).await;
    assert_eq!(q, f.queue("same", false).await);
    let req = |key: &str| bilibili::Input {
        video: "BV1xx411c7mD".into(),
        retry: false,
        request_id: key.into(),
    };
    assert!(
        bilibili::queue(&f.app, &f.user, req("another"), false)
            .await
            .is_err()
    );
    sqlx::query("UPDATE jobs SET status='cancelled' WHERE id=?")
        .bind(q["job_id"].as_str().unwrap())
        .execute(&f.app.pool)
        .await
        .unwrap();
    bilibili::recover(&f.app).await.unwrap();
    let cfg = bilibili::Config {
        daily_limit: 1,
        ..Default::default()
    };
    f.api(&f.user, "PUT", "/api/v2/bilibili", json!(cfg)).await;
    let new = f.queue("another", false).await;
    assert_ne!(q["id"], new["id"]);
    f.process(&new, false).await.unwrap();
    assert!(
        bilibili::queue(&f.app, &f.user, req("over-limit"), false)
            .await
            .is_err()
    );
}
#[tokio::test]
async fn schedules_obey_timezone_and_disabled_revision() {
    let f = Fixture::new().await;
    let cfg = bilibili::Config {
        enabled: true,
        keywords: vec!["猫咪".into()],
        ..Default::default()
    };
    let (s, v) = f.api(&f.user, "PUT", "/api/v2/bilibili", json!(cfg)).await;
    assert_eq!(s, StatusCode::OK, "{v}");
    sqlx::query("UPDATE bili_state SET next_run_at=? WHERE user_id=?")
        .bind(db::now() - 1)
        .bind(&f.user.id)
        .execute(&f.app.pool)
        .await
        .unwrap();
    bilibili::reconcile(&f.app).await.unwrap();
    bilibili::reconcile(&f.app).await.unwrap();
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM bili_runs")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(count, 1);
    f.api(
        &f.user,
        "PUT",
        "/api/v2/bilibili",
        json!(bilibili::Config::default()),
    )
    .await;
    let rid: String = sqlx::query_scalar("SELECT id FROM bili_runs")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert!(
        bilibili::process_with(
            &f.app,
            &f.user,
            &rid,
            &CancellationToken::new(),
            &Videos { captions: true }
        )
        .await
        .is_err()
    );
    assert!(f.model.calls.lock().unwrap().is_empty());
}

#[derive(Default)]
struct DiscoveryVideos {
    searches: Mutex<Vec<String>>,
    metadata: Mutex<Vec<VideoId>>,
    reads: Mutex<Vec<VideoId>>,
    many: bool,
}
impl Source for DiscoveryVideos {
    async fn search(&self, keyword: &str, _: i64) -> Result<Vec<VideoId>> {
        let mut searches = self.searches.lock().unwrap();
        let offset = if self.many { searches.len() * 20 } else { 0 };
        searches.push(keyword.into());
        Ok((0..if self.many { 20 } else { 2 })
            .map(|i| VideoId {
                bvid: "BV1xx411c7mD".into(),
                page: (offset + i + 1) as u32,
            })
            .collect())
    }
    async fn info(&self, id: &VideoId) -> Result<Video> {
        self.metadata.lock().unwrap().push(id.clone());
        Ok(Video {
            title: format!("猫咪的日常 P{}", id.page),
            author: "测试 UP 主".into(),
            duration: 10,
            cid: id.page as i64,
        })
    }
    async fn captions(&self, id: &VideoId, _: &Video) -> Result<Option<Captions>> {
        self.reads.lock().unwrap().push(id.clone());
        Videos { captions: true }
            .captions(id, &Video::default())
            .await
    }
}
impl Fixture {
    async fn autonomous(&self) {
        let cfg = bilibili::Config {
            autonomous: true,
            ..Default::default()
        };
        let (status, value) = self
            .api(&self.user, "PUT", "/api/v2/bilibili", json!(cfg))
            .await;
        assert_eq!(status, StatusCode::OK, "{value}");
        *self.model.plan.lock().unwrap() = Some(
            json!({"action":"explore","topics":["猫咪日常"],"reason":"想看看猫咪怎么晒太阳"})
                .to_string(),
        );
        *self.model.selection.lock().unwrap()=Some(json!({"picks":[{"index":1,"reason":"想看看第二条的日常观察"}],"reason":"只挑一条有兴趣的"}).to_string());
    }
    async fn discover(&self, key: &str) -> Value {
        bilibili::queue(
            &self.app,
            &self.user,
            bilibili::Input {
                video: String::new(),
                retry: false,
                request_id: key.into(),
            },
            false,
        )
        .await
        .unwrap()
    }
    async fn explore(&self, q: &Value, source: &DiscoveryVideos) -> Result<Value> {
        bilibili::process_with(
            &self.app,
            &self.user,
            q["id"].as_str().unwrap(),
            &CancellationToken::new(),
            source,
        )
        .await
    }
    async fn attempts(&self) -> i64 {
        sqlx::query_scalar("SELECT COALESCE(SUM(attempts),0) FROM bili_usage WHERE user_id=?")
            .bind(&self.user.id)
            .fetch_one(&self.app.pool)
            .await
            .unwrap()
    }
}

#[tokio::test]
async fn autonomous_can_schedule_without_keywords_but_old_fixed_mode_requires_them() {
    let f = Fixture::new().await;
    let old: bilibili::Config = serde_json::from_value(json!({"keywords":["猫咪"]})).unwrap();
    assert!(!old.autonomous);
    let fixed = bilibili::Config {
        enabled: true,
        ..Default::default()
    };
    assert_eq!(
        f.api(&f.user, "PUT", "/api/v2/bilibili", json!(fixed))
            .await
            .0,
        StatusCode::BAD_REQUEST
    );
    let cfg = bilibili::Config {
        enabled: true,
        autonomous: true,
        ..Default::default()
    };
    assert_eq!(
        f.api(&f.user, "PUT", "/api/v2/bilibili", json!(cfg))
            .await
            .0,
        StatusCode::OK
    );
    assert!(
        bilibili::config(&f.app, &f.user)
            .await
            .unwrap()
            .keywords
            .is_empty()
    );
}

#[tokio::test]
async fn model_selects_actual_second_candidate_and_records_why_without_duplicate_fetch() {
    let f = Fixture::new().await;
    f.autonomous().await;
    let source = DiscoveryVideos::default();
    let result = f
        .explore(&f.discover("choose-second").await, &source)
        .await
        .unwrap();
    assert_eq!(result["status"], "completed");
    assert!(result["report"].as_str().unwrap().contains("想看看第二条"));
    assert_eq!(*source.searches.lock().unwrap(), vec!["猫咪日常"]);
    assert_eq!(source.metadata.lock().unwrap().len(), 2);
    assert_eq!(source.reads.lock().unwrap()[0].page, 2);
    assert_eq!(f.attempts().await, 1);
    let calls = f.model.calls.lock().unwrap().clone();
    assert_eq!(calls.len(), 3);
    assert!(calls.iter().all(|v| v.get("tools").is_none()));
    let candidates: Value =
        serde_json::from_str(calls[1]["messages"][1]["content"].as_str().unwrap()).unwrap();
    assert_eq!(candidates["candidates"][1]["title"], "猫咪的日常 P2");
    assert!(!candidates.to_string().contains("测试字幕"));
    // Autonomous selection excludes the already attempted item on a later run.
    *f.model.selection.lock().unwrap() =
        Some(json!({"picks":[],"reason":"剩下这条暂时不想读"}).to_string());
    f.explore(&f.discover("exclude-seen").await, &source)
        .await
        .unwrap();
    let calls = f.model.calls.lock().unwrap().clone();
    let choices: Value =
        serde_json::from_str(calls[4]["messages"][1]["content"].as_str().unwrap()).unwrap();
    assert_eq!(choices["candidates"].as_array().unwrap().len(), 1);
    assert_eq!(choices["candidates"][0]["title"], "猫咪的日常 P1");
    assert_eq!(f.attempts().await, 1);
}

#[tokio::test]
async fn may_skip_topics_or_all_candidates_without_spending_reading_quota() {
    let f = Fixture::new().await;
    f.autonomous().await;
    let source = DiscoveryVideos::default();
    *f.model.plan.lock().unwrap() =
        Some(json!({"action":"skip","topics":[],"reason":"今天想安静待着"}).to_string());
    let result = f.explore(&f.discover("rest").await, &source).await.unwrap();
    assert!(result["report"].as_str().unwrap().contains("选择休息"));
    assert!(source.searches.lock().unwrap().is_empty());
    assert_eq!(f.attempts().await, 0);
    f.autonomous().await;
    *f.model.selection.lock().unwrap() =
        Some(json!({"picks":[],"reason":"没有想看的"}).to_string());
    let result = f
        .explore(&f.discover("nothing-interesting").await, &source)
        .await
        .unwrap();
    assert!(result["report"].as_str().unwrap().contains("不勉强挑选"));
    assert_eq!(source.metadata.lock().unwrap().len(), 2);
    assert!(source.reads.lock().unwrap().is_empty());
    assert_eq!(f.attempts().await, 0);
}

#[tokio::test]
async fn malformed_or_private_topics_never_reach_search() {
    let f = Fixture::new().await;
    f.autonomous().await;
    let source = DiscoveryVideos::default();
    let invalids = [
        json!({"action":"explore","topics":["2185888430"],"reason":"私人账号"}),
        json!({"action":"explore","topics":["https://example.com"],"reason":"链接"}),
        json!({"action":"explore","topics":["猫咪","猫咪"],"reason":"重复"}),
        json!({"action":"skip","topics":["猫咪"],"reason":"自相矛盾"}),
        json!({"action":"explore","topics":[],"reason":"空话题"}),
        json!({"action":"explore","topics":["猫咪"],"reason":"","command":"search"}),
    ];
    for (i, value) in invalids.iter().enumerate() {
        *f.model.plan.lock().unwrap() = Some(value.to_string());
        assert!(
            f.explore(&f.discover(&format!("bad-topic-{i}")).await, &source)
                .await
                .is_err()
        );
    }
    assert!(source.searches.lock().unwrap().is_empty());
    assert_eq!(f.attempts().await, 0);
}

#[tokio::test]
async fn invented_duplicate_or_over_budget_choices_do_not_fall_back_to_first_result() {
    let f = Fixture::new().await;
    f.autonomous().await;
    let cfg = bilibili::Config {
        autonomous: true,
        per_run: 1,
        ..Default::default()
    };
    f.api(&f.user, "PUT", "/api/v2/bilibili", json!(cfg)).await;
    let source = DiscoveryVideos::default();
    let invalids = [
        json!({"picks":[{"index":9,"reason":"不存在"}],"reason":"编造"}),
        json!({"picks":[{"index":0,"reason":"第一条"},{"index":0,"reason":"重复"}],"reason":"重复"}),
        json!({"picks":[{"index":0,"reason":"第一条"},{"index":1,"reason":"第二条"}],"reason":"超过上限"}),
    ];
    for (i, value) in invalids.iter().enumerate() {
        *f.model.selection.lock().unwrap() = Some(value.to_string());
        assert!(
            f.explore(&f.discover(&format!("bad-pick-{i}")).await, &source)
                .await
                .is_err()
        );
    }
    assert!(source.reads.lock().unwrap().is_empty());
    assert_eq!(f.attempts().await, 0);
}

#[tokio::test]
async fn exploration_bounds_candidates_and_respects_remaining_daily_quota() {
    let f = Fixture::new().await;
    f.autonomous().await;
    let source = DiscoveryVideos {
        many: true,
        ..Default::default()
    };
    let cfg = bilibili::Config {
        autonomous: true,
        daily_limit: 2,
        ..Default::default()
    };
    f.api(&f.user, "PUT", "/api/v2/bilibili", json!(cfg)).await;
    f.process(&f.queue("consume-one", false).await, false)
        .await
        .unwrap();
    *f.model.plan.lock().unwrap() = Some(
        json!({"action":"explore","topics":["猫咪","海洋","摄影"],"reason":"想探索不同的日常"})
            .to_string(),
    );
    f.explore(&f.discover("bounded").await, &source)
        .await
        .unwrap();
    assert!(source.searches.lock().unwrap().len() <= 3);
    assert_eq!(source.metadata.lock().unwrap().len(), 8);
    assert_eq!(source.reads.lock().unwrap().len(), 1);
    assert_eq!(f.attempts().await, 2);
    let calls = f.model.calls.lock().unwrap().clone();
    assert!(
        calls[1]["messages"][0]["content"]
            .as_str()
            .unwrap()
            .contains("最多选1条")
    );
}

#[tokio::test]
async fn explicit_video_bypasses_autonomous_planning_and_stale_config_stops_queued_work() {
    let f = Fixture::new().await;
    f.autonomous().await;
    let source = DiscoveryVideos::default();
    f.explore(&f.queue("explicit", false).await, &source)
        .await
        .unwrap();
    assert!(source.searches.lock().unwrap().is_empty());
    assert_eq!(f.model.calls.lock().unwrap().len(), 1);
    let q = f.discover("stale").await;
    f.api(
        &f.user,
        "PUT",
        "/api/v2/bilibili",
        json!(bilibili::Config::default()),
    )
    .await;
    assert!(f.explore(&q, &source).await.is_err());
    assert_eq!(f.model.calls.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn revocation_or_cancellation_during_planning_prevents_subsequent_external_work() {
    for change in ["config", "permission", "cancel"] {
        let f = Fixture::new().await;
        f.autonomous().await;
        let source = DiscoveryVideos::default();
        let q = f.discover(change).await;
        let token = CancellationToken::new();
        f.model
            .hold_plan
            .store(true, std::sync::atomic::Ordering::SeqCst);
        let work =
            bilibili::process_with(&f.app, &f.user, q["id"].as_str().unwrap(), &token, &source);
        let revoke = async {
            tokio::time::timeout(
                std::time::Duration::from_secs(5),
                f.model.entered.notified(),
            )
            .await
            .unwrap();
            match change {
                "config" => {
                    f.api(
                        &f.user,
                        "PUT",
                        "/api/v2/bilibili",
                        json!(bilibili::Config::default()),
                    )
                    .await;
                }
                "permission" => {
                    let mut config = lingxi::permissions::Config::default();
                    config
                        .features
                        .get_mut(&lingxi::permissions::Feature::Bilibili)
                        .unwrap()
                        .private = false;
                    f.api(&f.user, "PUT", "/api/v2/permissions", json!(config))
                        .await;
                }
                _ => token.cancel(),
            }
            f.model.resume.notify_one();
        };
        let (result, ()) = tokio::join!(work, revoke);
        assert!(result.is_err(), "{change}");
        assert!(source.searches.lock().unwrap().is_empty());
        assert_eq!(f.attempts().await, 0);
    }
}

async fn add_turn(f: &Fixture, user: &auth::User, content: &str, group: bool) {
    let cid = db::id();
    let tid = db::id();
    let at = db::now();
    sqlx::query(
        "INSERT INTO conversations(id,user_id,title,created_at,updated_at) VALUES(?,?,'test',?,?)",
    )
    .bind(&cid)
    .bind(&user.id)
    .bind(at)
    .bind(at)
    .execute(&f.app.pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,'user',?,?)",
    )
    .bind(&cid)
    .bind(content)
    .bind(at)
    .execute(&f.app.pool)
    .await
    .unwrap();
    sqlx::query("INSERT INTO turns(id,user_id,conversation_id,request_id,input_hash,status,created_at) VALUES(?,?,?,?,?,'completed',?)").bind(&tid).bind(&user.id).bind(&cid).bind(db::id()).bind(db::digest(content)).bind(at).execute(&f.app.pool).await.unwrap();
    if group {
        sqlx::query("INSERT INTO channel_bindings(user_id,channel,external_id,conversation_id) VALUES(?,'qq_owner',?,?)").bind(&user.id).bind(db::id()).bind(&cid).execute(&f.app.pool).await.unwrap();
    }
}

#[tokio::test]
async fn curiosity_uses_only_this_accounts_personal_context_and_respects_chat_scope() {
    let f = Fixture::new().await;
    f.autonomous().await;
    let other = auth::create_user(&f.app, "someone", "test-password-other".into(), false)
        .await
        .unwrap();
    add_turn(&f, &f.user, "PRIVATE_ORIGINAL 喜欢猫咪", false).await;
    add_turn(&f, &f.user, "GROUP_SECRET", true).await;
    add_turn(&f, &other, "OTHER_ACCOUNT_SECRET", false).await;
    *f.model.plan.lock().unwrap() =
        Some(json!({"action":"skip","topics":[],"reason":"今天休息"}).to_string());
    f.explore(
        &f.discover("private-context").await,
        &DiscoveryVideos::default(),
    )
    .await
    .unwrap();
    let input = f.model.calls.lock().unwrap()[0]["messages"][1]["content"]
        .as_str()
        .unwrap()
        .to_owned();
    assert!(input.contains("PRIVATE_ORIGINAL"));
    assert!(!input.contains("GROUP_SECRET") && !input.contains("OTHER_ACCOUNT_SECRET"));
    let mut config = lingxi::permissions::Config::default();
    config
        .features
        .get_mut(&lingxi::permissions::Feature::Chat)
        .unwrap()
        .private = false;
    f.api(&f.user, "PUT", "/api/v2/permissions", json!(config))
        .await;
    f.explore(
        &f.discover("no-chat-context").await,
        &DiscoveryVideos::default(),
    )
    .await
    .unwrap();
    assert!(
        !f.model.calls.lock().unwrap()[1]["messages"][1]["content"]
            .as_str()
            .unwrap()
            .contains("PRIVATE_ORIGINAL")
    );
}
