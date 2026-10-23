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
}
async fn model(State(m): State<Model>, Json(v): Json<Value>) -> impl IntoResponse {
    m.calls.lock().unwrap().push(v);
    let reply = m.reply.lock().unwrap().clone();
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
    let new = f.queue("another", false).await;
    assert_ne!(q["id"], new["id"]);
    let cfg = bilibili::Config {
        daily_limit: 1,
        ..Default::default()
    };
    f.api(&f.user, "PUT", "/api/v2/bilibili", json!(cfg)).await;
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
