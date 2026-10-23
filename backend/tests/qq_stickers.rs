use axum::{
    Json, Router,
    body::Body,
    extract::State,
    http::{Request, StatusCode},
    response::IntoResponse,
    routing::{get, post},
};
use base64::Engine;
use http_body_util::BodyExt;
use lingxi::{
    auth, db, integrations,
    qq::{self, stickers, store},
    settings,
    state::App,
};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    io::Cursor,
    sync::{Arc, Mutex},
};
use tokio_util::sync::CancellationToken;
use tower::ServiceExt;

fn picture(format: image::ImageFormat, color: u8) -> Vec<u8> {
    let img = image::DynamicImage::ImageRgba8(image::RgbaImage::from_pixel(
        48,
        40,
        image::Rgba([color, 180, 90, 255]),
    ));
    let mut bytes = Cursor::new(Vec::new());
    img.write_to(&mut bytes, format).unwrap();
    bytes.into_inner()
}
fn animated_picture() -> Vec<u8> {
    let mut bytes = Vec::new();
    {
        let mut encoder = image::codecs::gif::GifEncoder::new(&mut bytes);
        for index in 0..12 {
            encoder
                .encode_frame(image::Frame::from_parts(
                    image::RgbaImage::from_pixel(16, 16, image::Rgba([index * 20, 0, 0, 255])),
                    0,
                    0,
                    image::Delay::from_numer_denom_ms(100, 1),
                ))
                .unwrap();
        }
    }
    bytes
}
#[derive(Clone)]
struct Mock {
    requests: Arc<Mutex<Vec<Value>>>,
    decision: Arc<Mutex<String>>,
    responses: Arc<Mutex<VecDeque<String>>>,
    quote: Arc<Mutex<Value>>,
    png: Vec<u8>,
    second: Vec<u8>,
    gif: Vec<u8>,
}
async fn png(State(m): State<Mock>) -> impl IntoResponse {
    ([("content-type", "image/png")], m.png)
}
async fn second(State(m): State<Mock>) -> impl IntoResponse {
    ([("content-type", "image/png")], m.second)
}
async fn gif(State(m): State<Mock>) -> impl IntoResponse {
    ([("content-type", "image/gif")], m.gif)
}
async fn fake() -> impl IntoResponse {
    ([("content-type", "image/png")], "<html>not an image</html>")
}
async fn model(State(m): State<Mock>, Json(v): Json<Value>) -> impl IntoResponse {
    m.requests.lock().unwrap().push(v);
    let result = m
        .responses
        .lock()
        .unwrap()
        .pop_front()
        .unwrap_or_else(|| m.decision.lock().unwrap().clone());
    (
        [("content-type", "text/event-stream")],
        format!(
            "data: {}\n\ndata: [DONE]\n\n",
            json!({"choices":[{"delta":{"content":result}}]})
        ),
    )
}
async fn anthropic(State(m): State<Mock>, Json(v): Json<Value>) -> impl IntoResponse {
    m.requests.lock().unwrap().push(v);
    let result = m.decision.lock().unwrap().clone();
    (
        [("content-type", "text/event-stream")],
        format!(
            "data: {}\n\ndata: {}\n\n",
            json!({"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":result}}),
            json!({"type":"message_stop"})
        ),
    )
}
async fn quote(State(m): State<Mock>) -> Json<Value> {
    Json(json!({"status":"ok","retcode":0,"data":m.quote.lock().unwrap().clone()}))
}
struct Fixture {
    app: App,
    user: auth::User,
    mock: Mock,
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
        let pool = db::connect(&temp.path().join("main.sqlite3"))
            .await
            .unwrap();
        let app = App::new(
            pool,
            db::Secrets::from_key(&[6; 32]),
            temp.path().into(),
            true,
        )
        .unwrap();
        let user = auth::create_user(&app, "collector", "sticker-test-password".into(), false)
            .await
            .unwrap();
        let mock=Mock {requests:Default::default(),responses:Default::default(),decision:Arc::new(Mutex::new(json!({"collect":true,"confidence":0.91,"title":"清新的绿色画面","reason":"绿色画面简洁明快，看着很舒服","tags":["清新","绿色"]}).to_string())),quote:Default::default(),png:picture(image::ImageFormat::Png,90),second:picture(image::ImageFormat::Png,20),gif:animated_picture()};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let router = Router::new()
            .route("/pic.png", get(png))
            .route("/second.png", get(second))
            .route("/pic.gif", get(gif))
            .route("/fake.png", get(fake))
            .route("/v1/chat/completions", post(model))
            .route("/v1/messages", post(anthropic))
            .route("/get_msg", post(quote))
            .with_state(mock.clone());
        let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        settings::save_profiles(
            &app,
            &user,
            vec![settings::Profile {
                id: "vision".into(),
                name: "test vision".into(),
                protocol: "openai".into(),
                base_url: format!("{base}/v1"),
                api_key: "test".into(),
                model: "test".into(),
                context_window: 64000,
                max_output: 1024,
                system_prompt: "PRIVATE_OWNER_PROFILE_DO_NOT_SEND".into(),
            }],
        )
        .await
        .unwrap();
        integrations::save_config(&app,&user,json!({"qq":{"enabled":true,"owner_qq":"10001","bot_qq":"20001","groups":["30001"],"inbound":{"secret":"test-inbound-secret-more-than-32-characters"},"onebot":{"url":base,"token":"test"},"reply_mentioned":false,"collect_memory":false,"stickers":{"enabled":true,"auto_review":true}}})).await.unwrap();
        Self {
            app,
            user,
            mock,
            base,
            server,
            _temp: temp,
        }
    }
    async fn configure(&self, changes: Value) {
        let mut cfg = integrations::config(&self.app, &self.user).await.unwrap();
        for (k, v) in changes.as_object().unwrap() {
            cfg["qq"]["stickers"][k] = v.clone();
        }
        integrations::save_config(&self.app, &self.user, cfg)
            .await
            .unwrap();
    }
    async fn enable_private(&self, group_enabled: bool) {
        let mut cfg = integrations::config(&self.app, &self.user).await.unwrap();
        cfg["qq"]["private_enabled"] = json!(true);
        cfg["qq"]["enabled"] = json!(group_enabled);
        integrations::save_config(&self.app, &self.user, cfg)
            .await
            .unwrap();
    }
    async fn private_receive(&self, event: &str, qq_id: &str, text: &str, message: Value) -> usize {
        let cfg = qq::config(&self.app, &self.user).await.unwrap();
        stickers::receive_private(&self.app, &self.user, &cfg, event, qq_id, text, &message)
            .await
            .unwrap()
    }
    async fn private_pending(&self) -> Vec<String> {
        sqlx::query_scalar("SELECT id FROM sticker_inbox WHERE user_id=? AND status='pending' ORDER BY created_at,id").bind(&self.user.id).fetch_all(&self.app.pool).await.unwrap()
    }
    async fn private_process(&self, id: &str) -> lingxi::error::Result<Value> {
        stickers::process(
            &self.app,
            &self.user,
            &json!({"id":id,"scope":"owner_private"}),
            &CancellationToken::new(),
        )
        .await
    }
    async fn private_drain(&self) {
        for id in self.private_pending().await {
            self.private_process(&id).await.unwrap();
        }
    }
    fn event(&self, n: i64, qq: &str, text: &str, path: &str) -> Value {
        let mut segments = vec![];
        if !text.is_empty() {
            segments.push(json!({"type":"text","data":{"text":text}}));
        }
        if !path.is_empty() {
            segments.push(
                json!({"type":"image","data":{"url":format!("{}{path}",self.base),"sub_type":0}}),
            );
        }
        json!({"post_type":"message","message_type":"group","self_id":20001,"group_id":30001,"user_id":qq,"message_id":n,"sender":{"nickname":"主人"},"message":segments})
    }
    async fn ingest(&self, v: &Value) -> Value {
        qq::api::ingest(&self.app, &self.user, v).await.unwrap()
    }
    async fn pending(&self) -> Vec<String> {
        sqlx::query_scalar("SELECT id FROM sticker_inbox WHERE user_id=? AND status='pending' ORDER BY created_at,id").bind(&self.user.id).fetch_all(store::pool(&self.app).await.unwrap()).await.unwrap()
    }
    async fn process(&self, id: &str) -> lingxi::error::Result<Value> {
        stickers::process(
            &self.app,
            &self.user,
            &json!({"id":id}),
            &CancellationToken::new(),
        )
        .await
    }
    async fn drain(&self) {
        for id in self.pending().await {
            self.process(&id).await.unwrap();
        }
    }
    async fn items(&self) -> Value {
        stickers::list(
            State(self.app.clone()),
            self.user.clone(),
            axum::extract::Query(stickers::ListQuery::default()),
        )
        .await
        .unwrap()
        .0["data"]
            .clone()
    }
    async fn request(
        &self,
        method: &str,
        path: &str,
        user: Option<&auth::User>,
        value: Value,
    ) -> (StatusCode, Vec<u8>) {
        let mut builder = Request::builder()
            .method(method)
            .uri(path)
            .header("content-type", "application/json");
        if let Some(user) = user {
            let token = auth::issue_token(&self.app, user, "test", 3600)
                .await
                .unwrap();
            builder = builder.header("authorization", format!("Bearer {token}"));
        }
        let response = lingxi::api::router(self.app.clone())
            .oneshot(builder.body(Body::from(value.to_string())).unwrap())
            .await
            .unwrap();
        let status = response.status();
        (
            status,
            response
                .into_body()
                .collect()
                .await
                .unwrap()
                .to_bytes()
                .to_vec(),
        )
    }
}

#[tokio::test]
async fn model_sees_real_pixels_and_collects_only_once_per_delivery() {
    let f = Fixture::new().await;
    let event = f.event(1, "10002", "", "/pic.png");
    assert_eq!(f.ingest(&event).await["stickers_queued"], 1);
    f.ingest(&event).await;
    assert_eq!(f.pending().await.len(), 1);
    f.drain().await;
    let data = f.items().await;
    assert_eq!(data["total"], 1);
    assert_eq!(data["items"][0]["collected_by"], "model");
    assert_eq!(data["items"][0]["occurrences"], 1);
    let request = f.mock.requests.lock().unwrap()[0].clone();
    let url = request["messages"][1]["content"][1]["image_url"]["url"]
        .as_str()
        .unwrap();
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(url.strip_prefix("data:image/png;base64,").unwrap())
        .unwrap();
    assert_eq!(image::load_from_memory(&bytes).unwrap().width(), 48);
    assert!(!request.to_string().contains("PRIVATE_OWNER_PROFILE"));
    assert!(request.get("tools").is_none());
    let main_messages: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM messages")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(main_messages, 0);
    let main_facts: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM memories")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(main_facts, 0);
    f.ingest(&f.event(2, "10003", "", "/pic.png")).await;
    f.drain().await;
    assert_eq!(f.items().await["items"][0]["occurrences"], 2);
    assert_eq!(f.mock.requests.lock().unwrap().len(), 1);
    let id = data["items"][0]["id"].as_str().unwrap();
    let (status, original) = f
        .request(
            "GET",
            &format!("/api/v2/qq/stickers/{id}/image"),
            Some(&f.user),
            Value::Null,
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(original, f.mock.png);
    let jobs: Vec<String> = sqlx::query_scalar("SELECT params FROM jobs WHERE action='qq_sticker'")
        .fetch_all(&f.app.pool)
        .await
        .unwrap();
    assert!(!jobs.join("").contains("http"));
}

#[tokio::test]
async fn collection_reviews_ten_ordered_gif_frames_in_one_request() {
    let f = Fixture::new().await;
    f.ingest(&f.event(1, "10002", "", "/pic.gif")).await;
    f.drain().await;
    assert_eq!(f.items().await["total"], 1);
    let requests = f.mock.requests.lock().unwrap().clone();
    assert_eq!(requests.len(), 1);
    let content = requests[0]["messages"][1]["content"].as_array().unwrap();
    assert_eq!(content.len(), 21);
    assert!(
        content[0]["text"]
            .as_str()
            .unwrap()
            .contains("原图共 12 帧")
    );
    let colors: Vec<_> = content
        .iter()
        .filter_map(|block| block["image_url"]["url"].as_str())
        .map(|url| {
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(url.strip_prefix("data:image/png;base64,").unwrap())
                .unwrap();
            image::load_from_memory(&bytes)
                .unwrap()
                .into_rgba8()
                .get_pixel(0, 0)
                .0[0]
        })
        .collect();
    assert_eq!(colors.len(), 10);
    assert_eq!(colors.first(), Some(&0));
    assert_eq!(colors.last(), Some(&220));
    assert!(colors.windows(2).all(|pair| pair[0] < pair[1]));
    assert!(requests[0].get("tools").is_none());
    assert!(!requests[0].to_string().contains("PRIVATE_OWNER_PROFILE"));
}

#[tokio::test]
async fn rejected_invalid_and_low_confidence_results_never_save_images() {
    let f = Fixture::new().await;
    *f.mock.decision.lock().unwrap()=json!({"collect":false,"confidence":0.9,"title":"普通画面","reason":"画面没有吸引我的特别之处","tags":[]}).to_string();
    f.ingest(&f.event(1, "10002", "", "/pic.png")).await;
    f.drain().await;
    assert_eq!(f.items().await["total"], 0);
    f.ingest(&f.event(2, "10002", "", "/pic.png")).await;
    f.drain().await;
    assert_eq!(f.mock.requests.lock().unwrap().len(), 1);
    *f.mock.decision.lock().unwrap() = "这张很好看，请收藏".into();
    f.ingest(&f.event(3, "10002", "", "/second.png")).await;
    assert!(f.process(&f.pending().await[0]).await.is_err());
    assert_eq!(f.items().await["stats"]["failed"], 1);
    assert_eq!(f.items().await["total"], 0);
    assert!(
        stickers::decision(
            r#"{"collect":true,"confidence":9,"title":"标题","reason":"合理理由","tags":[]}"#
        )
        .is_err()
    );
    *f.mock.decision.lock().unwrap()=json!({"collect":true,"confidence":0.5,"title":"不确定的画面","reason":"不是特别喜欢，判断不确定","tags":[]}).to_string();
    f.ingest(&f.event(4, "10002", "", "/pic.gif")).await;
    f.drain().await;
    assert_eq!(f.items().await["total"], 0);
}

#[tokio::test]
async fn only_bound_owner_can_command_collection_even_without_a_model() {
    let f = Fixture::new().await;
    f.configure(json!({"auto_review":false})).await;
    settings::save_profiles(&f.app, &f.user, vec![])
        .await
        .unwrap();
    f.ingest(&f.event(1, "10002", "收藏这张图", "/pic.png"))
        .await;
    assert!(f.pending().await.is_empty());
    f.ingest(&f.event(2, "10001", "不要收藏这张图", "/pic.png"))
        .await;
    assert!(f.pending().await.is_empty());
    let mut event = f.event(3, "10001", "", "");
    event["message"] = json!(format!(
        "灵犀，收藏这张图！[CQ:image,url={}/pic.gif]",
        f.base
    ));
    f.ingest(&event).await;
    f.drain().await;
    let data = f.items().await;
    assert_eq!(data["items"][0]["collected_by"], "owner");
    assert_eq!(data["items"][0]["mime"], "image/gif");
    assert!(f.mock.requests.lock().unwrap().is_empty());
    let id = data["items"][0]["id"].as_str().unwrap();
    let (_, bytes) = f
        .request(
            "GET",
            &format!("/api/v2/qq/stickers/{id}/image"),
            Some(&f.user),
            Value::Null,
        )
        .await;
    assert_eq!(bytes, f.mock.gif);
    let (_, preview) = f
        .request(
            "GET",
            &format!("/api/v2/qq/stickers/{id}/image?thumbnail=true"),
            Some(&f.user),
            Value::Null,
        )
        .await;
    assert!(preview.starts_with(b"\x89PNG"));
}

#[tokio::test]
async fn group_gif_review_recovers_malformed_output_without_double_collection() {
    let f = Fixture::new().await;
    f.mock
        .responses
        .lock()
        .unwrap()
        .push_back("{\"collect\":".into());
    f.ingest(&f.event(91, "10002", "", "/pic.gif")).await;
    f.drain().await;
    assert_eq!(f.items().await["total"], 1);
    let requests = f.mock.requests.lock().unwrap().clone();
    assert_eq!(requests.len(), 2);
    for request in requests {
        assert_eq!(
            request["messages"][1]["content"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|v| v["type"] == "image_url")
                .count(),
            10
        );
    }
    let reviews: i64 =
        sqlx::query_scalar("SELECT SUM(count) FROM sticker_review_limits WHERE user_id=?")
            .bind(&f.user.id)
            .fetch_one(store::pool(&f.app).await.unwrap())
            .await
            .unwrap();
    assert_eq!(reviews, 1);
}

#[tokio::test]
async fn owner_can_collect_replied_images_but_never_cross_group() {
    let f = Fixture::new().await;
    f.configure(json!({"auto_review":false})).await;
    *f.mock.quote.lock().unwrap() = json!({"group_id":30001,"message":[{"type":"image","data":{"url":format!("{}/pic.png",f.base)}}]});
    let mut event = f.event(1, "10001", "收藏这张图", "");
    event["message"]
        .as_array_mut()
        .unwrap()
        .push(json!({"type":"reply","data":{"id":"999"}}));
    f.ingest(&event).await;
    f.drain().await;
    f.drain().await;
    assert_eq!(f.items().await["total"], 1);
    f.mock.quote.lock().unwrap()["group_id"] = json!(30002);
    event["message_id"] = json!(2);
    f.ingest(&event).await;
    assert!(f.process(&f.pending().await[0]).await.is_err());
    assert_eq!(f.items().await["total"], 1);
}

#[tokio::test]
async fn deletion_blocks_auto_recollection_tenant_access_and_allows_owner_restore() {
    let f = Fixture::new().await;
    f.ingest(&f.event(1, "10002", "", "/pic.png")).await;
    f.drain().await;
    let data = f.items().await;
    let id = data["items"][0]["id"].as_str().unwrap();
    let other = auth::create_user(&f.app, "other", "other-long-password".into(), false)
        .await
        .unwrap();
    let path = format!("/api/v2/qq/stickers/{id}");
    assert_eq!(
        f.request("DELETE", &path, Some(&other), Value::Null)
            .await
            .0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        f.request("GET", &format!("{path}/image"), Some(&other), Value::Null)
            .await
            .0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        f.request("GET", &format!("{path}/image"), None, Value::Null)
            .await
            .0,
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        f.request(
            "PATCH",
            &path,
            Some(&f.user),
            json!({"title":"我的小绿图","tags":["治愈"],"favorite":true})
        )
        .await
        .0,
        StatusCode::OK
    );
    let (_, body) = f
        .request(
            "GET",
            "/api/v2/qq/stickers?q=治愈&favorite=true",
            Some(&f.user),
            Value::Null,
        )
        .await;
    assert_eq!(
        serde_json::from_slice::<Value>(&body).unwrap()["data"]["total"],
        1
    );
    assert_eq!(
        f.request("DELETE", &path, Some(&f.user), Value::Null)
            .await
            .0,
        StatusCode::OK
    );
    f.ingest(&f.event(2, "10002", "", "/pic.png")).await;
    f.drain().await;
    assert_eq!(f.items().await["total"], 0);
    f.ingest(&f.event(3, "10001", "收藏这张图", "/pic.png"))
        .await;
    f.drain().await;
    assert_eq!(f.items().await["total"], 1);
    assert_eq!(f.mock.requests.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn quotas_group_filters_and_invalid_files_are_enforced() {
    let f = Fixture::new().await;
    f.configure(json!({"hourly_limit":1,"max_items":1})).await;
    let mut event = f.event(1, "10002", "", "/pic.png");
    event["group_id"] = json!(39999);
    f.ingest(&event).await;
    assert!(f.pending().await.is_empty());
    event["group_id"] = json!(30001);
    event["user_id"] = json!(20001);
    f.ingest(&event).await;
    assert!(f.pending().await.is_empty());
    f.ingest(&f.event(2, "10002", "", "/pic.png")).await;
    f.drain().await;
    f.ingest(&f.event(3, "10002", "", "/second.png")).await;
    f.drain().await;
    assert_eq!(f.mock.requests.lock().unwrap().len(), 1);
    assert_eq!(f.items().await["total"], 1);
    f.ingest(&f.event(4, "10001", "收藏这张图", "/second.png"))
        .await;
    assert!(f.process(&f.pending().await[0]).await.is_err());
    assert_eq!(f.items().await["total"], 1);
    f.ingest(&f.event(5, "10001", "收藏这张图", "/fake.png"))
        .await;
    assert!(f.process(&f.pending().await[0]).await.is_err());
    assert!(stickers::prepare(b"<svg onload='alert(1)'/>".to_vec()).is_err());
    assert!(stickers::prepare(vec![0; 8 * 1024 * 1024 + 1]).is_err());
    assert!(stickers::owner_command("/收藏"));
    assert!(!stickers::owner_command("有人说收藏这张图"));
}

#[tokio::test]
async fn anthropic_receives_native_image_blocks_and_small_context_counts_pixels() {
    let f = Fixture::new().await;
    let mut profiles = settings::profiles(&f.app, &f.user, true).await.unwrap();
    profiles[0].protocol = "anthropic".into();
    settings::save_profiles(&f.app, &f.user, profiles)
        .await
        .unwrap();
    f.ingest(&f.event(1, "10002", "", "/pic.png")).await;
    f.drain().await;
    let request = f.mock.requests.lock().unwrap()[0].clone();
    let image = &request["messages"][0]["content"][1];
    assert_eq!(image["type"], "image");
    assert_eq!(image["source"]["media_type"], "image/png");
    assert!(image["source"]["data"].as_str().unwrap().len() > 50);
    assert!(!request.to_string().contains("image_url"));
    assert_eq!(f.items().await["total"], 1);
    let estimate = lingxi::provider::token_estimate(
        &json!([{"role":"user","content":[{"type":"image_url","image_url":{"url":format!("data:image/png;base64,{}","a".repeat(1000000))}}]}]),
    );
    assert!(estimate < 3000);
}

#[tokio::test]
async fn interrupted_collection_can_be_retried_without_resetting_live_jobs() {
    let f = Fixture::new().await;
    f.ingest(&f.event(1, "10001", "收藏这张图", "/pic.png"))
        .await;
    sqlx::query("UPDATE jobs SET status='cancelled' WHERE action='qq_sticker'")
        .execute(&f.app.pool)
        .await
        .unwrap();
    stickers::reconcile(&f.app).await.unwrap();
    assert_eq!(f.items().await["stats"]["failed"], 1);
    let first = stickers::retry(State(f.app.clone()), f.user.clone())
        .await
        .unwrap();
    assert_eq!(first.0["data"]["queued"], 1);
    let second = stickers::retry(State(f.app.clone()), f.user.clone())
        .await
        .unwrap();
    assert_eq!(second.0["data"]["queued"], 0);
    f.drain().await;
    assert_eq!(f.items().await["total"], 1);
}

#[tokio::test]
async fn private_collection_isolated_from_groups_and_available_through_existing_api() {
    let f = Fixture::new().await;
    f.enable_private(true).await;
    let message = json!([{"type":"image","data":{"url":format!("{}/pic.png",f.base)}}]);
    assert_eq!(
        f.private_receive("same-event", "10001", "", message.clone())
            .await,
        1
    );
    assert_eq!(
        f.private_receive("same-event", "10001", "", message).await,
        1
    );
    assert_eq!(f.private_pending().await.len(), 1);
    assert!(f.pending().await.is_empty());
    let encrypted: String = sqlx::query_scalar("SELECT source FROM sticker_inbox LIMIT 1")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert!(!encrypted.contains(&f.base));
    assert!(f.process(&f.private_pending().await[0]).await.is_err()); // Old/group scope cannot consume private rows.
    f.private_drain().await;
    let data = f.items().await;
    assert_eq!(data["items"][0]["scope"], "owner_private");
    assert_eq!(data["items"][0]["group_id"], "");
    assert_eq!(data["items"][0]["collected_by"], "model");
    let private_id = data["items"][0]["id"].as_str().unwrap().to_owned();
    assert!(private_id.starts_with("owner-"));
    assert_eq!(
        stickers::reply_candidates(&f.app, &f.user, None)
            .await
            .unwrap()
            .len(),
        1
    );
    assert!(
        stickers::reply_candidates(&f.app, &f.user, Some("30001"))
            .await
            .unwrap()
            .is_empty()
    );
    assert!(
        stickers::reply_image(&f.app, &f.user, Some("30001"), &private_id)
            .await
            .is_err()
    );
    assert_eq!(
        stickers::reply_image(&f.app, &f.user, None, &private_id)
            .await
            .unwrap(),
        f.mock.png
    );
    f.ingest(&f.event(50, "10002", "", "/pic.png")).await;
    f.drain().await;
    assert_eq!(f.items().await["total"], 2); // Same pixels remain independently owned by each store.
    assert_eq!(f.mock.requests.lock().unwrap().len(), 2);
    assert_eq!(
        stickers::reply_candidates(&f.app, &f.user, None)
            .await
            .unwrap()
            .len(),
        1
    );
    let group_candidates = stickers::reply_candidates(&f.app, &f.user, Some("30001"))
        .await
        .unwrap();
    assert_eq!(group_candidates.len(), 1);
    let group_id = group_candidates[0]["id"].as_str().unwrap();
    assert!(
        stickers::reply_image(&f.app, &f.user, None, group_id)
            .await
            .is_err()
    );
    assert!(
        stickers::reply_image(&f.app, &f.user, Some("30002"), group_id)
            .await
            .is_err()
    );
    let (_, body) = f
        .request(
            "GET",
            "/api/v2/qq/stickers?scope=owner_private",
            Some(&f.user),
            Value::Null,
        )
        .await;
    assert_eq!(
        serde_json::from_slice::<Value>(&body).unwrap()["data"]["total"],
        1
    );
    let path = format!("/api/v2/qq/stickers/{private_id}");
    let other = auth::create_user(&f.app, "other", "other-sticker-password".into(), false)
        .await
        .unwrap();
    assert_eq!(
        f.request("GET", &format!("{path}/image"), Some(&other), Value::Null)
            .await
            .0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        f.request(
            "PATCH",
            &path,
            Some(&f.user),
            json!({"title":"主人的私藏","favorite":true})
        )
        .await
        .0,
        StatusCode::OK
    );
    let (status, image) = f
        .request("GET", &format!("{path}/image"), Some(&f.user), Value::Null)
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(image, f.mock.png);
    assert_eq!(
        f.request("DELETE", &path, Some(&f.user), Value::Null)
            .await
            .0,
        StatusCode::OK
    );
    assert!(
        stickers::reply_image(&f.app, &f.user, None, &private_id)
            .await
            .is_err()
    );
    assert_eq!(f.items().await["total"], 1);
}

#[tokio::test]
async fn private_collection_requires_owner_and_rechecks_config_before_processing() {
    let f = Fixture::new().await;
    f.enable_private(false).await;
    f.configure(json!({"auto_review":false})).await;
    settings::save_profiles(&f.app, &f.user, vec![])
        .await
        .unwrap();
    let message = json!([{"type":"image","data":{"url":format!("{}/pic.gif",f.base)}}]);
    assert_eq!(
        f.private_receive("outsider", "10002", "收藏这张图", message.clone())
            .await,
        0
    );
    assert_eq!(
        f.private_receive("no-command", "10001", "不要收藏这张图", message.clone())
            .await,
        0
    );
    assert_eq!(
        f.private_receive("owner", "10001", "收藏这张图", message.clone())
            .await,
        1
    );
    f.private_drain().await;
    assert_eq!(f.items().await["items"][0]["collected_by"], "owner");
    assert!(f.mock.requests.lock().unwrap().is_empty());
    assert!(f.pending().await.is_empty());
    assert_eq!(
        f.private_receive("revoked", "10001", "收藏这张图", message)
            .await,
        1
    );
    let mut cfg = integrations::config(&f.app, &f.user).await.unwrap();
    cfg["qq"]["private_enabled"] = json!(false);
    integrations::save_config(&f.app, &f.user, cfg)
        .await
        .unwrap();
    f.private_drain().await;
    let statuses: Vec<String> = sqlx::query_scalar("SELECT status FROM sticker_inbox ORDER BY id")
        .fetch_all(&f.app.pool)
        .await
        .unwrap();
    assert!(statuses.contains(&"skipped".into()));
    assert!(
        stickers::reply_candidates(&f.app, &f.user, None)
            .await
            .unwrap()
            .is_empty()
    );
}

#[tokio::test]
async fn private_quote_rejects_groups_and_other_contacts_and_retry_keeps_scope() {
    let f = Fixture::new().await;
    f.enable_private(false).await;
    f.configure(json!({"auto_review":false})).await;
    let message = json!([{"type":"reply","data":{"id":"500"}}]);
    *f.mock.quote.lock().unwrap() = json!({"message_type":"group","group_id":30001,"sender":{"user_id":10001},"message":[{"type":"image","data":{"url":format!("{}/pic.png",f.base)}}]});
    f.private_receive("quote-group", "10001", "/收藏", message.clone())
        .await;
    assert!(
        f.private_process(&f.private_pending().await[0])
            .await
            .is_err()
    );
    f.mock.quote.lock().unwrap()["message_type"] = json!("private");
    f.mock.quote.lock().unwrap()["group_id"] = Value::Null;
    f.mock.quote.lock().unwrap()["sender"]["user_id"] = json!(10002);
    f.private_receive("quote-other", "10001", "/收藏", message.clone())
        .await;
    assert!(
        f.private_process(&f.private_pending().await[0])
            .await
            .is_err()
    );
    f.mock.quote.lock().unwrap()["sender"]["user_id"] = json!(10001);
    sqlx::query("UPDATE jobs SET status='failed' WHERE action='qq_sticker'")
        .execute(&f.app.pool)
        .await
        .unwrap();
    let result = stickers::retry(State(f.app.clone()), f.user.clone())
        .await
        .unwrap();
    assert_eq!(result.0["data"]["queued"], 2);
    let scopes: Vec<String> = sqlx::query_scalar("SELECT json_extract(params,'$.scope') FROM jobs WHERE action='qq_sticker' AND status='queued'").fetch_all(&f.app.pool).await.unwrap();
    assert!(scopes.iter().all(|s| s == "owner_private"));
    f.private_drain().await; // Expand both quoted messages.
    f.private_drain().await; // Save / deduplicate their picture.
    assert_eq!(f.items().await["total"], 1);
    assert!(f.pending().await.is_empty());
    assert_eq!(f.items().await["items"][0]["scope"], "owner_private");
}

#[tokio::test]
async fn legacy_group_job_payload_survives_duplicate_delivery_reconcile_and_retry() {
    let f = Fixture::new().await;
    let event = f.event(700, "10001", "收藏这张图", "/pic.png");
    f.ingest(&event).await;
    let row = f.pending().await[0].clone();
    // Simulate a durable queue entry created by the previous release.
    sqlx::query("UPDATE jobs SET params=? WHERE action='qq_sticker'")
        .bind(json!({"id":row}).to_string())
        .execute(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(f.ingest(&event).await["accepted"], true);
    qq::worker::reconcile(&f.app).await.unwrap();
    let jobs: Vec<String> = sqlx::query_scalar("SELECT params FROM jobs WHERE action='qq_sticker'")
        .fetch_all(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(jobs.len(), 1);
    assert_eq!(
        serde_json::from_str::<Value>(&jobs[0]).unwrap(),
        json!({"id":row})
    );
    sqlx::query("UPDATE jobs SET status='cancelled' WHERE action='qq_sticker'")
        .execute(&f.app.pool)
        .await
        .unwrap();
    qq::worker::reconcile(&f.app).await.unwrap();
    assert_eq!(
        stickers::retry(State(f.app.clone()), f.user.clone())
            .await
            .unwrap()
            .0["data"]["queued"],
        1
    );
    let retry: String =
        sqlx::query_scalar("SELECT params FROM jobs WHERE action='qq_sticker' AND status='queued'")
            .fetch_one(&f.app.pool)
            .await
            .unwrap();
    assert_eq!(
        serde_json::from_str::<Value>(&retry).unwrap(),
        json!({"id":row})
    );
    f.drain().await;
    assert_eq!(f.items().await["total"], 1);
}

#[tokio::test]
async fn scheduler_recovers_private_stickers_without_a_group_database() {
    let f = Fixture::new().await;
    f.enable_private(false).await;
    f.configure(json!({"auto_review":false})).await;
    f.private_receive(
        "lost-private-job",
        "10001",
        "收藏这张图",
        json!([{"type":"image","data":{"url":format!("{}/pic.png",f.base)}}]),
    )
    .await;
    let row = f.private_pending().await[0].clone();
    sqlx::query("DELETE FROM jobs WHERE action='qq_sticker'")
        .execute(&f.app.pool)
        .await
        .unwrap();
    assert!(f.app.qq_pool.get().is_none());
    assert!(!f.app.data_dir.join("qq/people.sqlite3").exists());
    qq::worker::reconcile(&f.app).await.unwrap();
    let job: String =
        sqlx::query_scalar("SELECT params FROM jobs WHERE action='qq_sticker' AND status='queued'")
            .fetch_one(&f.app.pool)
            .await
            .unwrap();
    assert_eq!(
        serde_json::from_str::<Value>(&job).unwrap(),
        json!({"id":row,"scope":"owner_private"})
    );
    assert!(f.app.qq_pool.get().is_none());
    assert!(!f.app.data_dir.join("qq/people.sqlite3").exists());
    sqlx::query("UPDATE jobs SET status='cancelled' WHERE action='qq_sticker'")
        .execute(&f.app.pool)
        .await
        .unwrap();
    qq::worker::reconcile(&f.app).await.unwrap();
    assert_eq!(
        stickers::retry(State(f.app.clone()), f.user.clone())
            .await
            .unwrap()
            .0["data"]["queued"],
        1
    );
    f.private_drain().await;
    assert_eq!(f.items().await["total"], 1);
    assert!(f.app.qq_pool.get().is_none());
    assert!(!f.app.data_dir.join("qq/people.sqlite3").exists());
}
