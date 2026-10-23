use axum::{Json, Router, extract::State, response::IntoResponse, routing::post};
use base64::Engine;
use lingxi::{
    auth, db, integrations,
    qq::{self, dialogue, stickers, store},
    settings,
    state::App,
};
use serde_json::{Value, json};
use std::{
    io::Cursor,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};
use tokio_util::sync::CancellationToken;

#[derive(Clone, Default)]
struct Mock {
    requests: Arc<Mutex<Vec<Value>>>,
    sends: Arc<Mutex<Vec<Value>>>,
    decision: Arc<Mutex<String>>,
    fail_image: Arc<AtomicBool>,
    hold_model: Arc<AtomicBool>,
    model_entered: Arc<tokio::sync::Notify>,
    model_release: Arc<tokio::sync::Notify>,
    hold_text: Arc<AtomicBool>,
    text_entered: Arc<tokio::sync::Notify>,
    text_release: Arc<tokio::sync::Notify>,
}
async fn model(State(mock): State<Mock>, Json(value): Json<Value>) -> impl IntoResponse {
    mock.requests.lock().unwrap().push(value);
    if mock.hold_model.load(Ordering::SeqCst) {
        mock.model_entered.notify_one();
        mock.model_release.notified().await;
    }
    let decision = mock.decision.lock().unwrap().clone();
    (
        [("content-type", "text/event-stream")],
        format!(
            "data: {}\n\ndata: [DONE]\n\n",
            json!({"choices":[{"delta":{"content":decision}}]})
        ),
    )
}
async fn send(State(mock): State<Mock>, Json(value): Json<Value>) -> Json<Value> {
    let image = value["message"]
        .as_array()
        .unwrap()
        .iter()
        .any(|part| part["type"] == "image");
    mock.sends.lock().unwrap().push(value);
    if !image && mock.hold_text.load(Ordering::SeqCst) {
        mock.text_entered.notify_one();
        mock.text_release.notified().await;
    }
    Json(if image && mock.fail_image.load(Ordering::SeqCst) {
        json!({"status":"failed","retcode":100})
    } else {
        json!({"status":"ok","retcode":0,"data":{"message_id":123}})
    })
}
fn picture() -> Vec<u8> {
    let image = image::DynamicImage::new_rgba8(12, 12);
    let mut out = Cursor::new(Vec::new());
    image.write_to(&mut out, image::ImageFormat::Png).unwrap();
    out.into_inner()
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
            db::Secrets::from_key(&[7; 32]),
            temp.path().into(),
            true,
        )
        .unwrap();
        let user = auth::create_user(&app, "images", "image-test-password".into(), false)
            .await
            .unwrap();
        let mock = Mock::default();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let router = Router::new()
            .route("/v1/chat/completions", post(model))
            .route("/send_private_msg", post(send))
            .route("/send_group_msg", post(send))
            .with_state(mock.clone());
        let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        settings::save_profiles(
            &app,
            &user,
            vec![settings::Profile {
                id: "reply".into(),
                name: "reply test".into(),
                protocol: "openai".into(),
                base_url: format!("{base}/v1"),
                api_key: "test".into(),
                model: "test".into(),
                context_window: 16000,
                max_output: 1024,
                system_prompt: "PRIVATE_PERSONA_MUST_NOT_REACH_GROUP".into(),
            }],
        )
        .await
        .unwrap();
        integrations::save_config(&app,&user,json!({"qq":{"enabled":true,"private_enabled":true,"owner_qq":"10001","bot_qq":"20001","groups":["30001","30002"],"inbound":{"secret":"image-reply-test-key-at-least-32-characters"},"onebot":{"url":base,"token":"test"},"collect_memory":false,"reply_style":{"interval_secs":1},"stickers":{"enabled":true},"group_settings":{"30001":{"reply_style":{"enabled":true,"max_messages":2,"interval_secs":1}}}}})).await.unwrap();
        Self {
            app,
            user,
            mock,
            server,
            _temp: temp,
        }
    }
    async fn insert(&self, group: Option<&str>, id: &str, title: &str, uid: Option<&str>) {
        let pool = match group {
            Some(_) => store::pool(&self.app).await.unwrap(),
            None => &self.app.pool,
        };
        let image = picture();
        sqlx::query("INSERT INTO stickers(id,user_id,content_hash,title,tags,reason,collected_by,mime,width,height,byte_size,original,thumbnail,group_id,qq_id,created_at,updated_at) VALUES(?,?,?,?,?,'test','owner','image/png',12,12,?,?,?,?,?,1,1)")
            .bind(id).bind(uid.unwrap_or(&self.user.id)).bind(id).bind(title).bind("[\"可爱\"]").bind(image.len() as i64).bind(&image).bind(&image).bind(group.unwrap_or("")).bind("10001").execute(pool).await.unwrap();
    }
    async fn config(&self, group: Option<&str>) -> qq::Config {
        let cfg = qq::config(&self.app, &self.user).await.unwrap();
        group.map(|g| cfg.for_group(g).unwrap()).unwrap_or(cfg)
    }
    async fn send(
        &self,
        group: Option<&str>,
        key: &str,
        text: &str,
    ) -> lingxi::error::Result<usize> {
        dialogue::send(
            &self.app,
            &self.user,
            &self.config(group).await,
            group,
            group.map(|_| "10001"),
            key,
            text,
            group.is_none(),
            &CancellationToken::new(),
            None,
        )
        .await
    }
    fn decide(&self, id: Option<&str>) {
        *self.mock.decision.lock().unwrap() =
            json!({"sticker_id":id,"willingness":if id.is_some(){0.9}else{0.0}}).to_string();
    }
    fn sends(&self) -> Vec<Value> {
        self.mock.sends.lock().unwrap().clone()
    }
    fn requests(&self) -> Vec<Value> {
        self.mock.requests.lock().unwrap().clone()
    }
}

#[tokio::test]
async fn owner_reply_sends_saved_image_without_exposing_selection_or_changing_recipient() {
    let f = Fixture::new().await;
    f.insert(None, "owner-cute", "软乎乎的蓝鱼", None).await;
    f.insert(
        Some("30001"),
        "group-image",
        "GROUP_IMAGE_NOT_PRIVATE",
        None,
    )
    .await;
    f.decide(Some("owner-cute"));
    let text = "给你一只小鱼。[CQ:at,qq=99999]";
    assert_eq!(f.send(None, "private-once", text).await.unwrap(), 2);
    let sent = f.sends();
    assert_eq!(sent.len(), 2);
    assert!(
        sent.iter()
            .all(|value| value["user_id"] == 10001 && value["auto_escape"] == true)
    );
    assert_eq!(
        sent[0]["message"],
        json!([{"type":"text","data":{"text":text}}])
    );
    assert_eq!(sent[1]["message"][0]["type"], "image");
    let image = sent[1]["message"][0]["data"]["file"].as_str().unwrap();
    assert_eq!(
        base64::engine::general_purpose::STANDARD
            .decode(image.strip_prefix("base64://").unwrap())
            .unwrap(),
        picture()
    );
    let parts = dialogue::parts(&f.app, &f.user, "private-once", true)
        .await
        .unwrap();
    assert_eq!(parts[0]["body"], text);
    assert_eq!(parts[1]["body"], "[图片：软乎乎的蓝鱼]");
    assert!(parts.iter().all(|part| part["status"] == "sent"));
    assert!(!json!(parts).to_string().contains("base64"));
    let requests = json!(f.requests()).to_string();
    assert!(!requests.contains("GROUP_IMAGE_NOT_PRIVATE"));
    assert!(!requests.contains("PRIVATE_PERSONA_MUST_NOT_REACH_GROUP"));
    assert!(f.send(None, "private-once", text).await.is_err());
    assert_eq!(f.sends().len(), 2);
    assert_eq!(f.requests().len(), 1);
}

#[tokio::test]
async fn group_image_choices_are_scoped_and_preserve_text_bursts_and_mentions() {
    let f = Fixture::new().await;
    f.insert(None, "owner-secret", "PRIVATE_IMAGE_NEVER_GROUP", None)
        .await;
    f.insert(Some("30001"), "group-cute", "本群可爱小鱼", None)
        .await;
    f.insert(
        Some("30002"),
        "other-group",
        "OTHER_GROUP_NEVER_VISIBLE",
        None,
    )
    .await;
    f.insert(
        Some("30001"),
        "other-user",
        "OTHER_USER_NEVER_VISIBLE",
        Some("unrelated-user"),
    )
    .await;
    f.decide(Some("group-cute"));
    assert_eq!(
        f.send(Some("30001"), "group-once", "第一句\n\n第二句")
            .await
            .unwrap(),
        3
    );
    let sent = f.sends();
    assert!(
        sent.iter()
            .all(|value| value["group_id"] == "30001" && value.get("user_id").is_none())
    );
    assert_eq!(
        sent[0]["message"][0],
        json!({"type":"at","data":{"qq":"10001"}})
    );
    assert_eq!(sent[0]["message"][1]["data"]["text"], "第一句");
    assert_eq!(
        sent[1]["message"],
        json!([{"type":"text","data":{"text":"第二句"}}])
    );
    assert_eq!(sent[2]["message"][0]["type"], "image");
    let request = json!(f.requests()).to_string();
    for hidden in [
        "PRIVATE_IMAGE_NEVER_GROUP",
        "OTHER_GROUP_NEVER_VISIBLE",
        "OTHER_USER_NEVER_VISIBLE",
        "PRIVATE_PERSONA_MUST_NOT_REACH_GROUP",
    ] {
        assert!(!request.contains(hidden), "leaked {hidden}");
    }
    assert!(
        stickers::reply_image(&f.app, &f.user, Some("30001"), "owner-secret")
            .await
            .is_err()
    );
    assert!(
        stickers::reply_image(&f.app, &f.user, Some("30001"), "other-group")
            .await
            .is_err()
    );
}

#[tokio::test]
async fn empty_catalogue_disabled_collection_and_invalid_choices_fall_back_to_text() {
    let f = Fixture::new().await;
    assert_eq!(f.send(None, "empty", "你好").await.unwrap(), 1);
    assert!(f.requests().is_empty());
    f.insert(None, "owner-cute", "小鱼", None).await;
    for (i, result) in [
        r#"{"sticker_id":"https://evil.invalid/pic.png","willingness":0.99}"#,
        r#"{"sticker_id":"owner-cute","willingness":0.99,"user_id":"99999"}"#,
        "[CQ:image,file=file:///etc/passwd]",
        r#"{"sticker_id":null,"willingness":0.1}"#,
    ]
    .into_iter()
    .enumerate()
    {
        *f.mock.decision.lock().unwrap() = result.into();
        assert_eq!(
            f.send(None, &format!("invalid-{i}"), "你好").await.unwrap(),
            1
        );
    }
    let mut config = integrations::config(&f.app, &f.user).await.unwrap();
    config["qq"]["stickers"]["enabled"] = json!(false);
    integrations::save_config(&f.app, &f.user, config)
        .await
        .unwrap();
    f.decide(Some("owner-cute"));
    assert_eq!(f.send(None, "disabled", "你好").await.unwrap(), 1);
    assert_eq!(f.requests().len(), 4);
    assert!(
        f.sends()
            .iter()
            .all(|value| value["message"][0]["type"] == "text")
    );
}

#[tokio::test]
async fn uncertain_image_delivery_is_not_reselected_or_replayed() {
    let f = Fixture::new().await;
    f.insert(None, "owner-cute", "小鱼", None).await;
    f.decide(Some("owner-cute"));
    f.mock.fail_image.store(true, Ordering::SeqCst);
    assert!(f.send(None, "uncertain", "给你一只小鱼").await.is_err());
    let parts = dialogue::parts(&f.app, &f.user, "uncertain", true)
        .await
        .unwrap();
    assert_eq!(parts[0]["status"], "sent");
    assert_eq!(parts[1]["status"], "unknown");
    f.mock.fail_image.store(false, Ordering::SeqCst);
    assert!(f.send(None, "uncertain", "给你一只小鱼").await.is_err());
    assert_eq!(f.sends().len(), 2);
    assert_eq!(f.requests().len(), 1);
    // A new message must not repeat an image whose acknowledgement was uncertain.
    assert_eq!(f.send(None, "after-uncertain", "收到啦").await.unwrap(), 1);
    assert_eq!(f.requests().len(), 1);
    assert_eq!(f.sends().len(), 3);
}

#[tokio::test]
async fn repeated_replies_are_text_only_and_pacing_survives_app_recreation() {
    let f = Fixture::new().await;
    f.insert(None, "owner-cute", "小鱼", None).await;
    f.decide(Some("owner-cute"));
    assert_eq!(f.send(None, "first", "给你一只小鱼").await.unwrap(), 2);
    for n in 0..5 {
        assert_eq!(
            f.send(None, &format!("next-{n}"), "嗯，继续聊")
                .await
                .unwrap(),
            1
        );
    }
    assert_eq!(
        f.requests().len(),
        1,
        "cooldown must also skip model selection"
    );
    let app = App::new(
        f.app.pool.clone(),
        db::Secrets::from_key(&[7; 32]),
        f.app.data_dir.clone(),
        true,
    )
    .unwrap();
    assert_eq!(
        dialogue::send(
            &app,
            &f.user,
            &f.config(None).await,
            None,
            None,
            "after-restart",
            "还有一句",
            true,
            &CancellationToken::new(),
            None
        )
        .await
        .unwrap(),
        1
    );
    assert_eq!(f.requests().len(), 1);
}

#[tokio::test]
async fn image_requires_explicit_valid_willingness_of_at_least_seventy_five_percent() {
    let f = Fixture::new().await;
    f.insert(None, "owner-cute", "小鱼", None).await;
    for (n, decision) in [
        json!({"sticker_id":"owner-cute","willingness":0.749999}),
        json!({"sticker_id":"owner-cute"}),
        json!({"sticker_id":"owner-cute","willingness":75}),
        json!({"sticker_id":"owner-cute","willingness":"0.99"}),
        json!({"sticker_id":"owner-cute","willingness":-0.1}),
        json!({"sticker_id":null,"willingness":0.99}),
    ]
    .into_iter()
    .enumerate()
    {
        *f.mock.decision.lock().unwrap() = decision.to_string();
        assert_eq!(
            f.send(None, &format!("below-{n}"), "好呀").await.unwrap(),
            1
        );
    }
    *f.mock.decision.lock().unwrap() =
        json!({"sticker_id":"owner-cute","willingness":0.75}).to_string();
    assert_eq!(f.send(None, "threshold", "给你一只小鱼").await.unwrap(), 2);
    assert_eq!(
        f.sends()
            .iter()
            .filter(|v| v["message"][0]["type"] == "image")
            .count(),
        1
    );
}

#[tokio::test]
async fn elapsed_time_alone_cannot_bypass_text_turns_or_same_image_deduplication() {
    let f = Fixture::new().await;
    f.insert(None, "owner-first", "小鱼", None).await;
    f.decide(Some("owner-first"));
    assert_eq!(f.send(None, "first", "嘿嘿").await.unwrap(), 2);
    sqlx::query("UPDATE sticker_reply_turns SET image_at=image_at-301 WHERE image_at IS NOT NULL")
        .execute(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(f.send(None, "text-1", "嗯嗯").await.unwrap(), 1);
    assert_eq!(f.send(None, "text-2", "好呀").await.unwrap(), 1);
    // All candidates are recently used. Do not even ask the model to send them again.
    assert_eq!(f.send(None, "same-image", "再聊几句").await.unwrap(), 1);
    assert_eq!(f.requests().len(), 1);
    f.insert(None, "owner-other", "另一种表情", None).await;
    f.decide(Some("owner-other"));
    assert_eq!(f.send(None, "new-image", "这下开心了").await.unwrap(), 2);
    let requests = f.requests();
    assert!(!requests.last().unwrap().to_string().contains("owner-first"));
}

#[tokio::test]
async fn group_cadence_is_shared_across_owner_member_and_followup_but_not_other_destinations() {
    let f = Fixture::new().await;
    f.insert(Some("30001"), "group-first", "本群小鱼", None)
        .await;
    f.insert(Some("30002"), "group-second", "另一群的小鱼", None)
        .await;
    f.insert(None, "owner-private", "私人小鱼", None).await;
    f.decide(Some("group-first"));
    let cfg = f.config(Some("30001")).await;
    assert_eq!(
        dialogue::send(
            &f.app,
            &f.user,
            &cfg,
            Some("30001"),
            Some("10001"),
            "owner-group",
            "第一句\n\n第二句",
            true,
            &CancellationToken::new(),
            None
        )
        .await
        .unwrap(),
        3
    );
    assert_eq!(
        f.send(Some("30001"), "member-group", "接着说")
            .await
            .unwrap(),
        1
    );
    let anchor = qq::outreach::anchor(&f.app, &f.user, Some("30001"))
        .await
        .unwrap();
    assert_eq!(
        dialogue::send(
            &f.app,
            &f.user,
            &cfg,
            Some("30001"),
            None,
            "outreach:test",
            "补充一句",
            false,
            &CancellationToken::new(),
            Some(&anchor)
        )
        .await
        .unwrap(),
        1
    );
    assert_eq!(f.requests().len(), 1);
    let turns: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM sticker_reply_turns WHERE scope='30001'")
            .fetch_one(store::pool(&f.app).await.unwrap())
            .await
            .unwrap();
    assert_eq!(turns, 3, "two text parts count as one turn");
    f.decide(Some("group-second"));
    assert_eq!(
        f.send(Some("30002"), "other-group", "另一群你好")
            .await
            .unwrap(),
        2
    );
    f.decide(Some("owner-private"));
    assert_eq!(f.send(None, "private", "私聊你好").await.unwrap(), 2);
}

#[tokio::test]
async fn concurrent_image_decisions_only_allow_one_actual_image() {
    let f = Fixture::new().await;
    f.insert(None, "owner-cute", "小鱼", None).await;
    f.decide(Some("owner-cute"));
    f.mock.hold_model.store(true, Ordering::SeqCst);
    let (app, user, cfg) = (f.app.clone(), f.user.clone(), f.config(None).await);
    let one = tokio::spawn(async move {
        dialogue::send(
            &app,
            &user,
            &cfg,
            None,
            None,
            "concurrent-one",
            "第一句",
            true,
            &CancellationToken::new(),
            None,
        )
        .await
    });
    f.mock.model_entered.notified().await;
    let (app, user, cfg) = (f.app.clone(), f.user.clone(), f.config(None).await);
    let two = tokio::spawn(async move {
        dialogue::send(
            &app,
            &user,
            &cfg,
            None,
            None,
            "concurrent-two",
            "第二句",
            true,
            &CancellationToken::new(),
            None,
        )
        .await
    });
    f.mock.model_entered.notified().await;
    f.mock.model_release.notify_waiters();
    let results = [one.await.unwrap().unwrap(), two.await.unwrap().unwrap()];
    assert_eq!(results.iter().sum::<usize>(), 3);
    assert_eq!(
        f.sends()
            .iter()
            .filter(|v| v["message"][0]["type"] == "image")
            .count(),
        1
    );
}

#[tokio::test]
async fn deleting_selected_image_before_delivery_cancels_only_the_optional_part() {
    let f = Fixture::new().await;
    f.insert(None, "owner-cute", "小鱼", None).await;
    f.decide(Some("owner-cute"));
    f.mock.hold_text.store(true, Ordering::SeqCst);
    let (app, user, cfg) = (f.app.clone(), f.user.clone(), f.config(None).await);
    let task = tokio::spawn(async move {
        dialogue::send(
            &app,
            &user,
            &cfg,
            None,
            None,
            "deleted",
            "小鱼来了",
            true,
            &CancellationToken::new(),
            None,
        )
        .await
    });
    f.mock.text_entered.notified().await;
    sqlx::query("UPDATE stickers SET deleted_at=1,original=NULL WHERE id='owner-cute'")
        .execute(&f.app.pool)
        .await
        .unwrap();
    f.mock.text_release.notify_one();
    assert_eq!(task.await.unwrap().unwrap(), 1);
    assert_eq!(f.sends().len(), 1);
    let parts = dialogue::parts(&f.app, &f.user, "deleted", true)
        .await
        .unwrap();
    assert_eq!(parts[0]["status"], "sent");
    assert_eq!(parts[1]["status"], "cancelled");
}

#[tokio::test]
async fn new_message_during_image_selection_cancels_delayed_reply_before_any_send() {
    let f = Fixture::new().await;
    f.insert(None, "owner-cute", "小鱼", None).await;
    f.decide(Some("owner-cute"));
    f.mock.hold_model.store(true, Ordering::SeqCst);
    let (app, user, cfg) = (f.app.clone(), f.user.clone(), f.config(None).await);
    let anchor = qq::outreach::anchor(&app, &user, None).await.unwrap();
    let task = tokio::spawn(async move {
        dialogue::send(
            &app,
            &user,
            &cfg,
            None,
            None,
            "followup",
            "再补充一句",
            true,
            &CancellationToken::new(),
            Some(&anchor),
        )
        .await
    });
    f.mock.model_entered.notified().await;
    sqlx::query("INSERT INTO conversations(id,user_id,title,created_at,updated_at) VALUES('new-chat',?,'test',1,1)").bind(&f.user.id).execute(&f.app.pool).await.unwrap();
    sqlx::query("INSERT INTO messages(conversation_id,role,content,created_at) VALUES('new-chat','user','新消息',1)").execute(&f.app.pool).await.unwrap();
    f.mock.model_release.notify_one();
    assert!(task.await.unwrap().is_err());
    assert!(f.sends().is_empty());
    assert!(
        dialogue::parts(&f.app, &f.user, "followup", true)
            .await
            .unwrap()
            .is_empty()
    );
}
