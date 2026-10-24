use axum::{
    Json, Router,
    extract::State,
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use lingxi::{auth, companion::private, db, integrations, qq, settings, state::App};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    io::Cursor,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};
use tokio_util::sync::CancellationToken;

#[derive(Clone, Default)]
struct Mock {
    models: Arc<Mutex<Vec<Value>>>,
    pushes: Arc<Mutex<Vec<Value>>>,
    image_requests: Arc<Mutex<Vec<String>>>,
    vision_failure: Arc<AtomicBool>,
    vision_responses: Arc<Mutex<VecDeque<(StatusCode, String)>>>,
    chat_response: Arc<Mutex<Option<String>>>,
    hold_push: Arc<AtomicBool>,
    push_entered: Arc<tokio::sync::Notify>,
    push_release: Arc<tokio::sync::Notify>,
    large_original: Arc<Mutex<Vec<u8>>>,
}

async fn model(State(mock): State<Mock>, Json(body): Json<Value>) -> impl IntoResponse {
    let is_image = body["messages"]
        .as_array()
        .unwrap()
        .iter()
        .any(|m| m["content"].as_array().is_some());
    mock.models.lock().unwrap().push(body);
    if is_image && let Some((status, response)) = mock.vision_responses.lock().unwrap().pop_front()
    {
        return (status, [("content-type", "text/event-stream")], response);
    }
    if is_image && mock.vision_failure.load(Ordering::Relaxed) {
        return (
            StatusCode::BAD_REQUEST,
            [("content-type", "text/event-stream")],
            "vision unavailable".to_owned(),
        );
    }
    let text = if is_image {
        json!({"description":"一条蓝色圆滚滚的小鱼，正在微笑。"}).to_string()
    } else {
        mock.chat_response
            .lock()
            .unwrap()
            .clone()
            .unwrap_or_else(|| "这条蓝色小鱼看起来很开心呀。".into())
    };
    (
        StatusCode::OK,
        [("content-type", "text/event-stream")],
        format!(
            "data: {}\n\ndata: [DONE]\n\n",
            json!({"choices":[{"delta":{"content":text}}]})
        ),
    )
}

async fn push(State(mock): State<Mock>, Json(body): Json<Value>) -> Json<Value> {
    mock.pushes.lock().unwrap().push(body);
    if mock.hold_push.load(Ordering::SeqCst) {
        mock.push_entered.notify_one();
        mock.push_release.notified().await;
    }
    Json(json!({"status":"ok","retcode":0,"data":{"message_id":200}}))
}

fn image_bytes(format: image::ImageFormat) -> Vec<u8> {
    let mut bytes = Cursor::new(Vec::new());
    image::DynamicImage::new_rgb8(8, 8)
        .write_to(&mut bytes, format)
        .unwrap();
    bytes.into_inner()
}

async fn picture(State(mock): State<Mock>) -> impl IntoResponse {
    mock.image_requests.lock().unwrap().push("image".into());
    (
        [("content-type", "image/png")],
        image_bytes(image::ImageFormat::Png),
    )
}

async fn gif() -> impl IntoResponse {
    (
        [("content-type", "image/gif")],
        image_bytes(image::ImageFormat::Gif),
    )
}

fn animated_gif_bytes(frame_count: u32) -> Vec<u8> {
    let mut bytes = Vec::new();
    {
        let mut encoder = image::codecs::gif::GifEncoder::new(&mut bytes);
        encoder
            .encode_frames((0..frame_count).map(|index| {
                image::Frame::from_parts(
                    image::RgbaImage::from_pixel(
                        8,
                        8,
                        image::Rgba([(index * 19) as u8, 80, 200, 255]),
                    ),
                    0,
                    0,
                    image::Delay::from_numer_denom_ms(100, 1),
                )
            }))
            .unwrap();
    }
    bytes
}

async fn animated_gif() -> impl IntoResponse {
    ([("content-type", "image/gif")], animated_gif_bytes(12))
}

async fn short_gif() -> impl IntoResponse {
    ([("content-type", "image/gif")], animated_gif_bytes(3))
}

async fn oversized() -> impl IntoResponse {
    (
        [("content-type", "image/png")],
        vec![0u8; 32 * 1024 * 1024 + 1],
    )
}

async fn large_original(State(mock): State<Mock>) -> impl IntoResponse {
    (
        [("content-type", "image/png")],
        mock.large_original.lock().unwrap().clone(),
    )
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
        let app = App::new(
            db::connect(&temp.path().join("main.db")).await.unwrap(),
            db::Secrets::from_key(&[5; 32]),
            temp.path().into(),
            true,
        )
        .unwrap();
        let user = auth::create_user(&app, "owner", "private-image-tests".into(), false)
            .await
            .unwrap();
        let mock = Mock::default();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let router = Router::new()
            .route("/v1/chat/completions", post(model))
            .route("/send_private_msg", post(push))
            .route("/image", get(picture))
            .route("/gif", get(gif))
            .route("/animated-gif", get(animated_gif))
            .route("/short-gif", get(short_gif))
            .route("/broken", get(|| async { "not an image" }))
            .route("/oversized", get(oversized))
            .route("/large-original", get(large_original))
            .route("/expired", get(|| async { StatusCode::FORBIDDEN }))
            .route(
                "/corrupt",
                get(|| async { image_bytes(image::ImageFormat::Png)[..33].to_vec() }),
            )
            .with_state(mock.clone());
        let server = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        settings::save_profiles(
            &app,
            &user,
            vec![settings::Profile {
                id: "vision-test".into(),
                name: "test".into(),
                protocol: "openai".into(),
                base_url: format!("{base}/v1"),
                model: "vision".into(),
                api_key: "test".into(),
                context_window: 20000,
                max_output: 2048,
                system_prompt: "温柔小鱼".into(),
            }],
        )
        .await
        .unwrap();
        integrations::save_config(
            &app,
            &user,
            json!({"qq":{
                "private_enabled":true,"owner_qq":"10001","bot_qq":"20001",
                "onebot":{"url":base,"token":"test"},
                "inbound":{"secret":"private-image-test-secret-longer-than-32"}
            }}),
        )
        .await
        .unwrap();
        Self {
            app,
            user,
            mock,
            base,
            server,
            _temp: temp,
        }
    }
    fn event(&self, id: i64, message: Value) -> Value {
        json!({"post_type":"message","message_type":"private","self_id":20001,"user_id":10001,"message_id":id,"message":message})
    }
    fn image(&self, path: &str, kind: &str) -> Value {
        json!({"type":kind,"data":{"url":format!("{}{path}?signed=private-source-token",self.base)}})
    }
    async fn count(&self, table: &str) -> i64 {
        sqlx::query_scalar(&format!("SELECT COUNT(*) FROM {table}"))
            .fetch_one(&self.app.pool)
            .await
            .unwrap()
    }
    async fn process(&self, id: i64) -> Value {
        let key = db::digest(&format!("private:{}:20001:{id}", self.user.id));
        private::process_qq(
            &self.app,
            &self.user,
            &json!({"id":key}),
            &CancellationToken::new(),
        )
        .await
        .unwrap()
    }
    async fn history(&self) -> String {
        let rows: Vec<String> = sqlx::query_scalar("SELECT content FROM messages ORDER BY id")
            .fetch_all(&self.app.pool)
            .await
            .unwrap();
        rows.join("\n")
    }
}

#[tokio::test]
async fn burst_images_and_text_make_one_turn_with_an_image_memory_anchor() {
    let f = Fixture::new().await;
    for event in [
        f.event(901, json!([{"type":"text","data":{"text":"先看这张图"}}])),
        f.event(902, json!([f.image("/image", "image")])),
        f.event(
            903,
            json!([{"type":"text","data":{"text":"最后一起告诉我看到了什么"}}]),
        ),
    ] {
        qq::api::ingest(&f.app, &f.user, &event).await.unwrap();
    }
    sqlx::query("UPDATE qq_private_events SET created_at=?")
        .bind(db::now() - 6)
        .execute(&f.app.pool)
        .await
        .unwrap();
    let result = f.process(901).await;
    assert_eq!(result["batched"], 3);
    assert_eq!(f.mock.pushes.lock().unwrap().len(), 1);
    assert_eq!(
        f.mock.models.lock().unwrap().len(),
        2,
        "one vision request and one combined chat request"
    );
    let history = f.history().await;
    assert_eq!(history.matches("先看这张图").count(), 1);
    assert_eq!(history.matches("最后一起告诉我看到了什么").count(), 1);
    assert!(history.contains("蓝色圆滚滚"));
    let anchor: String = sqlx::query_scalar("SELECT request_id FROM turns")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    let image_key = db::digest(&format!("private:{}:20001:902", f.user.id));
    assert_eq!(anchor, format!("qq-private:{image_key}"));
    assert_eq!(f.process(902).await["duplicate"], true);
    assert_eq!(f.process(903).await["duplicate"], true);
    assert_eq!(f.mock.pushes.lock().unwrap().len(), 1);
    let count: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM qq_private_events WHERE status='sent'")
            .fetch_one(&f.app.pool)
            .await
            .unwrap();
    assert_eq!(count, 3);
    assert!(!f.app.data_dir.join("qq/people.sqlite3").exists());
}

#[tokio::test]
async fn cancelled_wait_leaves_private_burst_pending_without_model_or_send() {
    let f = Fixture::new().await;
    let event = f.event(
        904,
        json!([{"type":"text","data":{"text":"稍等我还没说完"}}]),
    );
    qq::api::ingest(&f.app, &f.user, &event).await.unwrap();
    let key = db::digest(&format!("private:{}:20001:904", f.user.id));
    let cancel = CancellationToken::new();
    cancel.cancel();
    assert!(
        private::process_qq(&f.app, &f.user, &json!({"id":key}), &cancel)
            .await
            .is_err()
    );
    assert!(f.mock.models.lock().unwrap().is_empty());
    assert!(f.mock.pushes.lock().unwrap().is_empty());
    let status: String = sqlx::query_scalar("SELECT status FROM qq_private_events")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(status, "pending");
}

#[tokio::test]
async fn private_burst_limits_total_images_and_keeps_the_rest_pending() {
    let f = Fixture::new().await;
    let mut profiles = settings::profiles(&f.app, &f.user, true).await.unwrap();
    profiles[0].context_window = 128000;
    settings::save_profiles(&f.app, &f.user, profiles)
        .await
        .unwrap();
    for id in 910..915 {
        qq::api::ingest(
            &f.app,
            &f.user,
            &f.event(id, json!([f.image("/image", "image")])),
        )
        .await
        .unwrap();
    }
    sqlx::query("UPDATE qq_private_events SET created_at=?")
        .bind(db::now() - 6)
        .execute(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(f.process(910).await["batched"], 4);
    assert_eq!(f.mock.image_requests.lock().unwrap().len(), 4);
    assert_eq!(f.mock.pushes.lock().unwrap().len(), 1);
    let pending: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM qq_private_events WHERE status='pending'")
            .fetch_one(&f.app.pool)
            .await
            .unwrap();
    assert_eq!(pending, 1);
    assert_eq!(f.process(914).await["batched"], 1);
    assert_eq!(f.mock.image_requests.lock().unwrap().len(), 5);
    assert_eq!(f.mock.pushes.lock().unwrap().len(), 2);
}

#[tokio::test]
async fn interrupted_private_parts_only_update_their_own_assistant_record() {
    let f = Fixture::new().await;
    let reply = "第一段\n\n第二段\n\n第三段";
    *f.mock.chat_response.lock().unwrap() = Some(reply.into());
    f.mock.hold_push.store(true, Ordering::SeqCst);
    let mut cfg = integrations::config(&f.app, &f.user).await.unwrap();
    cfg["qq"]["reply_style"] = json!({"enabled":true,"max_messages":3,"interval_secs":2});
    integrations::save_config(&f.app, &f.user, cfg)
        .await
        .unwrap();
    qq::api::ingest(
        &f.app,
        &f.user,
        &f.event(920, json!([{"type":"text","data":{"text":"请分段回答"}}])),
    )
    .await
    .unwrap();
    sqlx::query("UPDATE qq_private_events SET created_at=?")
        .bind(db::now() - 6)
        .execute(&f.app.pool)
        .await
        .unwrap();
    let (app, user) = (f.app.clone(), f.user.clone());
    let key = db::digest(&format!("private:{}:20001:920", f.user.id));
    let task = tokio::spawn(async move {
        private::process_qq(&app, &user, &json!({"id":key}), &CancellationToken::new()).await
    });
    f.mock.push_entered.notified().await;
    let cid: String = sqlx::query_scalar(
        "SELECT conversation_id FROM private_bindings WHERE user_id=? AND channel='onebot'",
    )
    .bind(&f.user.id)
    .fetch_one(&f.app.pool)
    .await
    .unwrap();
    let web = lingxi::chat::prepare(
        &f.app,
        &f.user,
        &cid,
        lingxi::chat::Send {
            message: "网页里另问一句".into(),
            request_id: "web-same-reply".into(),
        },
    )
    .await
    .unwrap();
    lingxi::chat::run(f.app.clone(), web, None, CancellationToken::new())
        .await
        .unwrap();
    qq::api::ingest(
        &f.app,
        &f.user,
        &f.event(921, json!([{"type":"text","data":{"text":"等等还有补充"}}])),
    )
    .await
    .unwrap();
    f.mock.push_release.notify_one();
    assert_eq!(task.await.unwrap().unwrap()["sent"], true);
    let assistants: Vec<String> = sqlx::query_scalar(
        "SELECT content FROM messages WHERE conversation_id=? AND role='assistant' ORDER BY id",
    )
    .bind(&cid)
    .fetch_all(&f.app.pool)
    .await
    .unwrap();
    assert_eq!(assistants, vec!["第一段", reply]);
    let web_reply: String =
        sqlx::query_scalar("SELECT reply FROM turns WHERE request_id='web-same-reply'")
            .fetch_one(&f.app.pool)
            .await
            .unwrap();
    assert_eq!(web_reply, reply);
    assert_eq!(f.mock.pushes.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn pure_images_are_seen_once_and_encrypted_only_in_main_database() {
    let f = Fixture::new().await;
    let event = f.event(1, json!([f.image("/image", "image")]));
    for _ in 0..2 {
        assert_eq!(
            qq::api::ingest(&f.app, &f.user, &event).await.unwrap()["accepted"],
            true
        );
    }
    assert_eq!(f.count("qq_private_events").await, 1);
    assert_eq!(f.count("jobs").await, 1);
    let before: String = sqlx::query_scalar("SELECT media FROM qq_private_events")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert!(!before.contains("private-source-token"));
    assert!(
        f.app
            .secrets
            .decrypt(&before)
            .unwrap()
            .contains("private-source-token")
    );
    assert_eq!(f.process(1).await["sent"], true);
    assert_eq!(f.process(1).await["duplicate"], true);
    assert_eq!(f.mock.image_requests.lock().unwrap().len(), 1);
    assert_eq!(f.mock.pushes.lock().unwrap().len(), 1);
    assert_eq!(f.mock.models.lock().unwrap().len(), 2);
    let requests = f.mock.models.lock().unwrap().clone();
    assert!(
        requests[0]["messages"][1]["content"][1]["image_url"]["url"]
            .as_str()
            .unwrap()
            .starts_with("data:image/png;base64,")
    );
    assert!(requests[1].to_string().contains("一条蓝色圆滚滚的小鱼"));
    assert!(requests[1].to_string().contains("不是主人的自述"));
    assert!(
        !requests
            .iter()
            .any(|v| v.to_string().contains("private-source-token"))
    );
    let saved: String = sqlx::query_scalar("SELECT media FROM qq_private_events")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    let decoded: Value = serde_json::from_str(&f.app.secrets.decrypt(&saved).unwrap()).unwrap();
    assert_eq!(
        STANDARD
            .decode(decoded["images"][0]["original"].as_str().unwrap())
            .unwrap(),
        image_bytes(image::ImageFormat::Png)
    );
    assert!(!f.history().await.contains("private-source-token"));
    assert_eq!(f.count("messages").await, 2);
    assert!(!f.app.data_dir.join("qq/people.sqlite3").exists());
}

#[tokio::test]
async fn original_over_eight_mib_is_downloaded_and_only_bounded_preview_reaches_model() {
    let f = Fixture::new().await;
    let mut random = 37u32;
    let image = image::RgbImage::from_fn(1800, 1800, |_, _| {
        let mut channel = || {
            random = random.wrapping_mul(1664525).wrapping_add(1013904223);
            (random >> 24) as u8
        };
        image::Rgb([channel(), channel(), channel()])
    });
    let mut original = Cursor::new(Vec::new());
    image::DynamicImage::ImageRgb8(image)
        .write_to(&mut original, image::ImageFormat::Png)
        .unwrap();
    let original = original.into_inner();
    assert!(original.len() > 8 * 1024 * 1024);
    *f.mock.large_original.lock().unwrap() = original.clone();
    qq::api::ingest(
        &f.app,
        &f.user,
        &f.event(101, json!([f.image("/large-original", "image")])),
    )
    .await
    .unwrap();
    assert_eq!(f.process(101).await["sent"], true);
    let requests = f.mock.models.lock().unwrap().clone();
    assert_eq!(requests.len(), 2);
    let url = requests[0]["messages"][1]["content"][1]["image_url"]["url"]
        .as_str()
        .unwrap();
    let model_image = STANDARD
        .decode(url.strip_prefix("data:image/jpeg;base64,").unwrap())
        .unwrap();
    assert!(model_image.len() <= 384 * 1024);
    let decoded = image::load_from_memory(&model_image).unwrap();
    // Random noise may require a second resize to honor the strict byte cap.
    assert!((512..=1024).contains(&decoded.width()));
    assert_eq!(decoded.width(), decoded.height());
    let raw: String = sqlx::query_scalar("SELECT media FROM qq_private_events")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    let media: Value = serde_json::from_str(&f.app.secrets.decrypt(&raw).unwrap()).unwrap();
    assert_eq!(
        STANDARD
            .decode(media["images"][0]["original"].as_str().unwrap())
            .unwrap(),
        original
    );
    assert_eq!(
        media["images"][0]["description"],
        "一条蓝色圆滚滚的小鱼，正在微笑。"
    );
    assert_eq!(f.mock.pushes.lock().unwrap().len(), 1);
    assert!(!f.app.data_dir.join("qq/people.sqlite3").exists());
}

#[tokio::test]
async fn image_loading_failures_explain_safe_stage_and_never_expose_source_url() {
    for (path, code, hint) in [
        ("/expired", "image_download_http", "HTTP 403"),
        ("/broken", "image_format", "图片格式不支持"),
        ("/corrupt", "image_decode", "图片损坏"),
        ("/oversized", "image_size", "32 MiB"),
    ] {
        let f = Fixture::new().await;
        qq::api::ingest(
            &f.app,
            &f.user,
            &f.event(102, json!([f.image(path, "image")])),
        )
        .await
        .unwrap();
        f.process(102).await;
        let raw: String = sqlx::query_scalar("SELECT media FROM qq_private_events")
            .fetch_one(&f.app.pool)
            .await
            .unwrap();
        let media: Value = serde_json::from_str(&f.app.secrets.decrypt(&raw).unwrap()).unwrap();
        assert_eq!(media["images"][0]["failure_code"], code);
        assert_eq!(media["images"][0]["vision_attempts"], 0);
        assert!(media["images"][0]["error"].as_str().unwrap().contains(hint));
        assert!(!f.history().await.contains("private-source-token"));
        assert!(f.mock.models.lock().unwrap().is_empty());
    }
}

#[tokio::test]
async fn image_input_rejects_non_owner_wrong_bot_and_ephemeral_flash_before_download() {
    let f = Fixture::new().await;
    let mut event = f.event(1, json!([f.image("/image", "image")]));
    event["user_id"] = json!(99999);
    assert_eq!(
        qq::api::ingest(&f.app, &f.user, &event).await.unwrap()["ignored"],
        true
    );
    event["user_id"] = json!(10001);
    event["self_id"] = json!(99999);
    assert_eq!(
        qq::api::ingest(&f.app, &f.user, &event).await.unwrap()["ignored"],
        true
    );
    event["self_id"] = json!(20001);
    event["message"][0]["data"]["type"] = json!("flash");
    assert_eq!(
        qq::api::ingest(&f.app, &f.user, &event).await.unwrap()["ignored"],
        true
    );
    assert_eq!(f.count("qq_private_events").await, 0);
    assert!(f.mock.image_requests.lock().unwrap().is_empty());
    assert!(f.mock.models.lock().unwrap().is_empty());
}

#[tokio::test]
async fn text_mface_gif_and_image_limit_preserve_visible_scope() {
    let f = Fixture::new().await;
    let mut message = vec![
        json!({"type":"text","data":{"text":"看看这些表情"}}),
        f.image("/gif", "mface"),
    ];
    message.extend((0..4).map(|_| f.image("/image", "image")));
    qq::api::ingest(&f.app, &f.user, &f.event(2, json!(message)))
        .await
        .unwrap();
    f.process(2).await;
    let history = f.history().await;
    assert!(history.contains("看看这些表情"));
    assert!(history.contains("GIF 共 1 帧，本次按时间顺序识别全部 1 帧"));
    assert!(history.contains("另有 1 张图片超过单条4张上限"));
    assert_eq!(f.mock.image_requests.lock().unwrap().len(), 3);
    assert_eq!(f.mock.models.lock().unwrap().len(), 5);
}

#[tokio::test]
async fn private_gifs_send_ten_chronological_frames_and_archive_the_actual_sampling_scope() {
    for (path, total_frames, sampled_frames) in [("/animated-gif", 12, 10), ("/short-gif", 3, 3)] {
        let f = Fixture::new().await;
        let mut profiles = settings::profiles(&f.app, &f.user, false).await.unwrap();
        profiles[0].context_window = 64000;
        settings::save_profiles(&f.app, &f.user, profiles)
            .await
            .unwrap();
        qq::api::ingest(
            &f.app,
            &f.user,
            &f.event(1, json!([f.image(path, "image")])),
        )
        .await
        .unwrap();
        assert_eq!(f.process(1).await["sent"], true);
        let requests = f.mock.models.lock().unwrap().clone();
        assert_eq!(requests.len(), 2);
        let content = requests[0]["messages"][1]["content"].as_array().unwrap();
        let images: Vec<_> = content
            .iter()
            .filter(|item| item["type"] == "image_url")
            .collect();
        assert_eq!(images.len(), sampled_frames);
        let mut decoded_frame_colors = Vec::new();
        for image in images {
            let encoded = image["image_url"]["url"]
                .as_str()
                .unwrap()
                .strip_prefix("data:image/png;base64,")
                .unwrap();
            let decoded = image::load_from_memory(&STANDARD.decode(encoded).unwrap())
                .unwrap()
                .to_rgba8();
            decoded_frame_colors.push(decoded.get_pixel(0, 0)[0]);
        }
        assert!(
            decoded_frame_colors
                .windows(2)
                .all(|pair| pair[0] < pair[1])
        );
        assert_eq!(decoded_frame_colors.first(), Some(&0));
        assert_eq!(
            decoded_frame_colors.last(),
            Some(&(((total_frames - 1) * 19) as u8))
        );
        assert_eq!(content.len(), 1 + sampled_frames * 2);
        for (index, pair) in content[1..].chunks_exact(2).enumerate() {
            assert_eq!(pair[0]["type"], "text");
            let original_index = usize::from(decoded_frame_colors[index]) / 19;
            assert_eq!(
                pair[0]["text"],
                format!(
                    "GIF 原第 {} 帧 / 共 {total_frames} 帧，时间 {} 毫秒：",
                    original_index + 1,
                    original_index * 100
                )
            );
            assert_eq!(pair[1]["type"], "image_url");
        }
        assert!(!requests[0].to_string().contains("private-source-token"));
        let encrypted: String = sqlx::query_scalar("SELECT media FROM qq_private_events")
            .fetch_one(&f.app.pool)
            .await
            .unwrap();
        let saved: Value =
            serde_json::from_str(&f.app.secrets.decrypt(&encrypted).unwrap()).unwrap();
        assert_eq!(saved["images"][0]["sampled_frames"], sampled_frames);
        assert_eq!(saved["images"][0]["total_frames"], total_frames);
        assert_eq!(
            STANDARD
                .decode(saved["images"][0]["original"].as_str().unwrap())
                .unwrap(),
            animated_gif_bytes(total_frames as u32)
        );
        let history = f.history().await;
        assert!(history.contains(&format!("GIF 共 {total_frames} 帧")));
        let expected_scope = if total_frames == sampled_frames {
            format!("按时间顺序识别全部 {sampled_frames} 帧")
        } else {
            format!("按时间顺序抽样识别 {sampled_frames} 帧")
        };
        assert!(history.contains(&expected_scope));
        assert!(requests[1].to_string().contains("不是主人的自述"));
        assert!(!f.app.data_dir.join("qq/people.sqlite3").exists());
    }
}

#[tokio::test]
async fn cq_image_strings_are_understood_without_exposing_signed_urls() {
    let f = Fixture::new().await;
    let message = format!(
        "看看这张[CQ:image,url={}/image?signed=private-source-token&amp;x=1]",
        f.base
    );
    qq::api::ingest(&f.app, &f.user, &f.event(3, json!(message)))
        .await
        .unwrap();
    f.process(3).await;
    assert!(f.history().await.contains("看看这张"));
    assert!(f.history().await.contains("蓝色圆滚滚"));
    assert!(!f.history().await.contains("private-source-token"));
}

#[tokio::test]
async fn unreadable_or_unsupported_images_produce_explicit_reply_without_chat_guessing() {
    for path in ["/broken", "/oversized"] {
        let f = Fixture::new().await;
        qq::api::ingest(
            &f.app,
            &f.user,
            &f.event(1, json!([f.image(path, "image")])),
        )
        .await
        .unwrap();
        assert_eq!(f.process(1).await["sent"], true);
        assert!(f.mock.models.lock().unwrap().is_empty());
        assert_eq!(f.mock.pushes.lock().unwrap().len(), 1);
        assert!(f.history().await.contains("未识别内容"));
        assert!(f.history().await.contains("没能识别内容"));
        assert_eq!(f.count("messages").await, 2);
    }
    let f = Fixture::new().await;
    f.mock.vision_failure.store(true, Ordering::Relaxed);
    qq::api::ingest(
        &f.app,
        &f.user,
        &f.event(1, json!([f.image("/image", "mface")])),
    )
    .await
    .unwrap();
    f.process(1).await;
    assert_eq!(f.mock.models.lock().unwrap().len(), 1);
    assert!(f.history().await.contains("当前看图模型未能识别"));
    assert!(!f.history().await.contains("蓝色小鱼"));
}

#[tokio::test]
async fn untrusted_private_image_origin_and_local_file_never_reach_model() {
    let f = Fixture::new().await;
    let message = json!([
        {"type":"image","data":{"url":"http://127.0.0.1:9/private-secret"}},
        {"type":"image","data":{"file":"../../secret.png"}}
    ]);
    qq::api::ingest(&f.app, &f.user, &f.event(1, message))
        .await
        .unwrap();
    f.process(1).await;
    assert!(f.mock.models.lock().unwrap().is_empty());
    assert!(f.mock.image_requests.lock().unwrap().is_empty());
    assert!(!f.history().await.contains("private-secret"));
    assert!(!f.history().await.contains("secret.png"));
}

#[tokio::test]
async fn disabling_private_chat_before_processing_prevents_image_download_and_reply() {
    let f = Fixture::new().await;
    qq::api::ingest(
        &f.app,
        &f.user,
        &f.event(1, json!([f.image("/image", "image")])),
    )
    .await
    .unwrap();
    let mut cfg = integrations::config(&f.app, &f.user).await.unwrap();
    cfg["qq"]["private_enabled"] = json!(false);
    integrations::save_config(&f.app, &f.user, cfg)
        .await
        .unwrap();
    let key = db::digest(&format!("private:{}:20001:1", f.user.id));
    assert!(
        private::process_qq(
            &f.app,
            &f.user,
            &json!({"id":key}),
            &CancellationToken::new()
        )
        .await
        .is_err()
    );
    assert!(f.mock.image_requests.lock().unwrap().is_empty());
    assert!(f.mock.pushes.lock().unwrap().is_empty());
}

#[tokio::test]
async fn attachment_descriptions_do_not_become_persona_evidence_but_plain_text_still_does() {
    let f = Fixture::new().await;
    sqlx::query("INSERT INTO persona_growth(user_id,profile_id,enabled,created_at) VALUES(?,'vision-test',1,?)")
        .bind(&f.user.id).bind(db::now()).execute(&f.app.pool).await.unwrap();
    qq::api::ingest(
        &f.app,
        &f.user,
        &f.event(1, json!([f.image("/image", "image")])),
    )
    .await
    .unwrap();
    f.process(1).await;
    qq::api::ingest(
        &f.app,
        &f.user,
        &f.event(
            2,
            json!([
                {"type":"text","data":{"text":"请看看这个，不要当作我的性格"}},
                f.image("/image","image")
            ]),
        ),
    )
    .await
    .unwrap();
    f.process(2).await;
    assert_eq!(f.count("persona_observations").await, 0);
    qq::api::ingest(
        &f.app,
        &f.user,
        &f.event(
            3,
            json!([
                {"type":"text","data":{"text":"以后请对我说话简单一点"}}
            ]),
        ),
    )
    .await
    .unwrap();
    f.process(3).await;
    assert_eq!(f.count("persona_observations").await, 1);
}

#[tokio::test]
async fn separate_vision_profile_works_with_collection_disabled_and_partial_image_failure() {
    let f = Fixture::new().await;
    let mut profiles = settings::profiles(&f.app, &f.user, false).await.unwrap();
    let mut vision = profiles[0].clone();
    vision.id = "image-model".into();
    vision.model = "separate-vision".into();
    vision.api_key = "vision-test-key".into();
    profiles.push(vision);
    settings::save_profiles(&f.app, &f.user, profiles)
        .await
        .unwrap();
    let mut cfg = integrations::config(&f.app, &f.user).await.unwrap();
    cfg["qq"]["profile_id"] = json!("vision-test");
    cfg["qq"]["stickers"] = json!({"enabled":false,"profile_id":"image-model"});
    integrations::save_config(&f.app, &f.user, cfg)
        .await
        .unwrap();
    qq::api::ingest(
        &f.app,
        &f.user,
        &f.event(
            1,
            json!([
                {"type":"text","data":{"text":"两张都看一下"}},
                f.image("/image","image"),f.image("/broken","image")
            ]),
        ),
    )
    .await
    .unwrap();
    f.process(1).await;
    let requests = f.mock.models.lock().unwrap().clone();
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[0]["model"], "separate-vision");
    assert_eq!(requests[0]["max_tokens"], 2048);
    assert_eq!(requests[1]["model"], "vision");
    assert!(
        requests[1]
            .to_string()
            .contains("本次图片处理状态需如实告知用户")
    );
    let history = f.history().await;
    assert!(history.contains("图片 1：一条蓝色"));
    assert!(history.contains("图片 2：图片格式不支持"));
    assert!(history.contains("未识别内容"));
    assert!(!f.app.data_dir.join("qq/people.sqlite3").exists());
}

#[tokio::test]
async fn disabled_vision_does_not_download_or_send_images_to_model() {
    use lingxi::permissions::{Config, Feature};
    let f = Fixture::new().await;
    let event = f.event(71, json!([f.image("/image", "image")]));
    qq::api::ingest(&f.app, &f.user, &event).await.unwrap();
    // Revoke after receipt: the queued message must use the latest permissions.
    let mut cfg = Config::default();
    cfg.features.get_mut(&Feature::Vision).unwrap().private = false;
    settings::put(&f.app, &f.user, "feature_permissions", &json!(cfg))
        .await
        .unwrap();
    f.process(71).await;
    assert!(f.mock.image_requests.lock().unwrap().is_empty());
    assert!(
        f.mock
            .models
            .lock()
            .unwrap()
            .iter()
            .all(|v| !v.to_string().contains("image_url"))
    );
    assert!(!f.history().await.contains("一条蓝色圆滚滚的小鱼"));
    let mut qqcfg = qq::config(&f.app, &f.user).await.unwrap();
    qqcfg.stickers.enabled = true;
    cfg.features
        .get_mut(&Feature::StickerCollection)
        .unwrap()
        .private = false;
    settings::put(&f.app, &f.user, "feature_permissions", &json!(cfg))
        .await
        .unwrap();
    assert_eq!(
        qq::stickers::receive_private(
            &f.app,
            &f.user,
            &qqcfg,
            "collection-revoked",
            "10001",
            "收藏这张图片",
            &json!([f.image("/image", "image")])
        )
        .await
        .unwrap(),
        0
    );
}

fn vision_stream(text: &str, finish: Option<&str>, done: bool) -> String {
    format!(
        "data: {}\n\n{}",
        json!({"choices":[{"delta":{"content":text},"finish_reason":finish}]}),
        if done { "data: [DONE]\n\n" } else { "" }
    )
}

#[tokio::test]
async fn transient_vision_failures_retry_once_without_duplicate_chat_or_send() {
    for response in [
        (
            StatusCode::SERVICE_UNAVAILABLE,
            "sensitive upstream failure".into(),
        ),
        (
            StatusCode::OK,
            vision_stream("{\"description\":", None, true),
        ),
        (
            StatusCode::OK,
            vision_stream("{\"description\":", None, false),
        ),
        (
            StatusCode::OK,
            vision_stream("{\"description\":", Some("length"), true),
        ),
    ] {
        let f = Fixture::new().await;
        f.mock.vision_responses.lock().unwrap().push_back(response);
        qq::api::ingest(
            &f.app,
            &f.user,
            &f.event(81, json!([f.image("/image", "image")])),
        )
        .await
        .unwrap();
        assert_eq!(f.process(81).await["sent"], true);
        assert_eq!(f.mock.models.lock().unwrap().len(), 3); // two observations, one chat
        assert_eq!(f.mock.pushes.lock().unwrap().len(), 1);
        assert_eq!(f.mock.image_requests.lock().unwrap().len(), 1);
        assert_eq!(f.count("messages").await, 2);
        assert!(f.history().await.contains("一条蓝色圆滚滚的小鱼"));
        assert!(!f.history().await.contains("sensitive upstream"));
        let media: String = sqlx::query_scalar("SELECT media FROM qq_private_events")
            .fetch_one(&f.app.pool)
            .await
            .unwrap();
        let media: Value = serde_json::from_str(&f.app.secrets.decrypt(&media).unwrap()).unwrap();
        assert_eq!(media["images"][0]["vision_attempts"], 2);
        assert_eq!(media["images"][0]["failure_code"], "");
    }
}

#[tokio::test]
async fn wrapped_observation_and_extra_fields_do_not_discard_a_readable_picture() {
    let f = Fixture::new().await;
    let raw = "观察结果：\n```JSON\n{\"description\":\"一条蓝色的小鱼\",\"confidence\":0.9}\n```";
    f.mock
        .vision_responses
        .lock()
        .unwrap()
        .push_back((StatusCode::OK, vision_stream(raw, None, true)));
    qq::api::ingest(
        &f.app,
        &f.user,
        &f.event(82, json!([f.image("/image", "image")])),
    )
    .await
    .unwrap();
    f.process(82).await;
    assert_eq!(f.mock.models.lock().unwrap().len(), 2);
    assert!(f.history().await.contains("一条蓝色的小鱼"));
    assert!(!f.history().await.contains("没能识别"));
}

#[tokio::test]
async fn gif_retry_preserves_all_ten_frames_and_uses_configured_output_budget() {
    let f = Fixture::new().await;
    let mut profiles = settings::profiles(&f.app, &f.user, false).await.unwrap();
    profiles[0].context_window = 64000;
    profiles[0].max_output = 8192;
    settings::save_profiles(&f.app, &f.user, profiles)
        .await
        .unwrap();
    f.mock
        .vision_responses
        .lock()
        .unwrap()
        .push_back((StatusCode::BAD_GATEWAY, String::new()));
    qq::api::ingest(
        &f.app,
        &f.user,
        &f.event(86, json!([f.image("/animated-gif", "image")])),
    )
    .await
    .unwrap();
    f.process(86).await;
    let requests = f.mock.models.lock().unwrap().clone();
    assert_eq!(requests.len(), 3);
    for request in &requests[..2] {
        assert_eq!(request["max_tokens"], 4096);
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
    assert_eq!(
        requests[0]["messages"][1]["content"],
        requests[1]["messages"][1]["content"]
    );
    assert_eq!(f.mock.pushes.lock().unwrap().len(), 1);
    assert!(f.history().await.contains("抽样识别 10 帧"));
}

#[tokio::test]
async fn permanent_and_exhausted_failures_have_honest_distinct_reasons() {
    for (status, calls, code, message) in [
        (StatusCode::UNAUTHORIZED, 1, "authentication", "认证失败"),
        (
            StatusCode::SERVICE_UNAVAILABLE,
            2,
            "upstream",
            "重试后仍未成功",
        ),
    ] {
        let f = Fixture::new().await;
        f.mock
            .vision_responses
            .lock()
            .unwrap()
            .extend((0..2).map(|_| (status, "private error body".into())));
        qq::api::ingest(
            &f.app,
            &f.user,
            &f.event(83, json!([f.image("/image", "image")])),
        )
        .await
        .unwrap();
        f.process(83).await;
        assert_eq!(f.mock.models.lock().unwrap().len(), calls);
        assert_eq!(f.mock.pushes.lock().unwrap().len(), 1);
        assert!(f.history().await.contains(message));
        assert!(!f.history().await.contains("private error body"));
        let raw: String = sqlx::query_scalar("SELECT media FROM qq_private_events")
            .fetch_one(&f.app.pool)
            .await
            .unwrap();
        let media: Value = serde_json::from_str(&f.app.secrets.decrypt(&raw).unwrap()).unwrap();
        assert_eq!(media["images"][0]["failure_code"], code);
        assert_eq!(media["images"][0]["vision_attempts"], calls);
    }
}

#[tokio::test]
async fn revoking_vision_during_retry_delay_stops_the_second_request() {
    use lingxi::permissions::{Config, Feature};
    let f = Fixture::new().await;
    f.mock
        .vision_responses
        .lock()
        .unwrap()
        .push_back((StatusCode::SERVICE_UNAVAILABLE, String::new()));
    qq::api::ingest(
        &f.app,
        &f.user,
        &f.event(84, json!([f.image("/image", "image")])),
    )
    .await
    .unwrap();
    let change = async {
        tokio::time::timeout(std::time::Duration::from_secs(8), async {
            while f.mock.models.lock().unwrap().is_empty() {
                tokio::time::sleep(std::time::Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        let mut cfg = Config::default();
        cfg.features.get_mut(&Feature::Vision).unwrap().private = false;
        settings::put(&f.app, &f.user, "feature_permissions", &json!(cfg))
            .await
            .unwrap();
    };
    let (result, ()) = tokio::join!(f.process(84), change);
    assert_eq!(result["sent"], true);
    assert_eq!(f.mock.models.lock().unwrap().len(), 1);
    assert!(f.history().await.contains("图片识别权限已关闭"));
}

#[tokio::test]
async fn cancelling_vision_retry_never_sends_or_generates_a_chat() {
    let f = Fixture::new().await;
    f.mock
        .vision_responses
        .lock()
        .unwrap()
        .push_back((StatusCode::SERVICE_UNAVAILABLE, String::new()));
    qq::api::ingest(
        &f.app,
        &f.user,
        &f.event(85, json!([f.image("/image", "image")])),
    )
    .await
    .unwrap();
    let cancel = CancellationToken::new();
    let change = async {
        tokio::time::timeout(std::time::Duration::from_secs(8), async {
            while f.mock.models.lock().unwrap().is_empty() {
                tokio::time::sleep(std::time::Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        cancel.cancel();
    };
    let params = json!({"id":db::digest(&format!("private:{}:20001:85", f.user.id))});
    let (result, ()) = tokio::join!(
        private::process_qq(&f.app, &f.user, &params, &cancel),
        change
    );
    assert!(matches!(result, Err(lingxi::error::Error::Cancelled)));
    assert_eq!(f.mock.models.lock().unwrap().len(), 1);
    assert!(f.mock.pushes.lock().unwrap().is_empty());
    assert_eq!(f.count("messages").await, 0);
}

#[tokio::test]
async fn small_context_keeps_over_budget_burst_messages_pending() {
    let f = Fixture::new().await;
    let mut profiles = settings::profiles(&f.app, &f.user, false).await.unwrap();
    profiles[0].context_window = 8192;
    settings::save_profiles(&f.app, &f.user, profiles)
        .await
        .unwrap();
    let mut permissions = lingxi::permissions::Config::default();
    for (feature, access) in &mut permissions.features {
        if *feature != lingxi::permissions::Feature::Chat {
            access.private = false;
        }
    }
    settings::put(&f.app, &f.user, "feature_permissions", &json!(permissions))
        .await
        .unwrap();
    for id in [221, 222] {
        qq::api::ingest(
            &f.app,
            &f.user,
            &f.event(
                id,
                json!([{"type":"text","data":{"text":"测试".repeat(1000)}}]),
            ),
        )
        .await
        .unwrap();
    }
    assert_eq!(f.process(221).await["batched"], 1);
    let pending: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM qq_private_events WHERE status='pending'")
            .fetch_one(&f.app.pool)
            .await
            .unwrap();
    assert_eq!(pending, 1);
    assert_eq!(f.mock.models.lock().unwrap().len(), 1);
    assert_eq!(f.mock.pushes.lock().unwrap().len(), 1);
}
