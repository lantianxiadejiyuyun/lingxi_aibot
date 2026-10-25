use axum::{Json, extract::State};
use lingxi::{
    auth, db,
    interaction::{self, Input},
    settings,
    state::App,
};
use serde_json::{Value, json};

struct Fixture {
    app: App,
    user: auth::User,
    _dir: tempfile::TempDir,
}
impl Fixture {
    async fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let app = App::new(
            db::connect(&dir.path().join("test.db")).await.unwrap(),
            db::Secrets::from_key(&[3; 32]),
            dir.path().into(),
            true,
        )
        .unwrap();
        let user = auth::create_user(&app, "owner", "long-test-password".into(), false)
            .await
            .unwrap();
        Self {
            app,
            user,
            _dir: dir,
        }
    }
    async fn view(&self) -> Value {
        interaction::view(State(self.app.clone()), self.user.clone())
            .await
            .unwrap()
            .0["data"]
            .clone()
    }
    async fn save(&self, mode: &str, revision: &Value) -> lingxi::error::Result<Value> {
        interaction::save(
            State(self.app.clone()),
            self.user.clone(),
            Json(
                serde_json::from_value::<Input>(json!({"mode":mode,"revision":revision})).unwrap(),
            ),
        )
        .await
        .map(|v| v.0["data"].clone())
    }
    async fn seed(&self, integrations: &Value) {
        settings::put(
            &self.app,
            &self.user,
            "integrations",
            &json!(self.app.secrets.encrypt(&integrations.to_string()).unwrap()),
        )
        .await
        .unwrap();
    }
}
#[tokio::test]
async fn preset_updates_real_delivery_limits_without_enabling_channels_or_overwriting_secrets() {
    let f = Fixture::new().await;
    let mut qq = lingxi::qq::Config::default();
    qq.groups = vec!["30001".into(), "30002".into()];
    qq.owner_qq = "10001".into();
    qq.bot_qq = "10002".into();
    qq.onebot.token = "onebot-secret".into();
    qq.profile_id = Some("keep-model".into());
    qq.followup.enabled = true;
    let mut g = lingxi::qq::sessions::GroupSettings::default();
    g.enabled = false;
    g.system_prompt = "PUBLIC_PERSONA".into();
    g.memory_profile_id = Some("keep-member-model".into());
    g.followup.enabled = true;
    g.proactive.enabled = true;
    g.decision.mode = lingxi::qq::decision::Mode::Observe;
    g.reply_mention_sender = Some(false);
    qq.group_settings.insert("30001".into(), g.clone());
    let before = json!({"qq":qq,"search":{"url":"https://example.org","token":"search-secret"},"channels":{"feishu_app":{"secret":"feishu-secret"}}});
    f.seed(&before).await;
    let permission = json!({"unchanged":"permissions"});
    settings::put(&f.app, &f.user, "feature_permissions", &permission)
        .await
        .unwrap();
    let initial = f.view().await;
    assert_eq!(initial["configured"], false);
    assert!(!initial.to_string().contains("secret"));
    let saved = f.save("natural", &initial["revision"]).await.unwrap();
    assert_eq!(saved["configured"], true);
    assert_eq!(saved["customized"], false);
    let after = lingxi::integrations::config(&f.app, &f.user).await.unwrap();
    assert_eq!(after["search"], before["search"]);
    assert_eq!(after["channels"], before["channels"]);
    let cfg: lingxi::qq::Config = serde_json::from_value(after["qq"].clone()).unwrap();
    assert!(!cfg.enabled && !cfg.private_enabled);
    assert_eq!(cfg.onebot.token, "onebot-secret");
    assert_eq!(cfg.profile_id, qq.profile_id);
    assert!(cfg.reply_style.enabled);
    assert_eq!(cfg.reply_style.max_messages, 2);
    assert!(cfg.followup.enabled);
    assert_eq!(cfg.followup.max_followups, 1);
    let applied = &cfg.group_settings["30001"];
    assert!(!applied.enabled);
    assert_eq!(applied.system_prompt, g.system_prompt);
    assert_eq!(applied.memory_profile_id, g.memory_profile_id);
    assert_eq!(applied.decision.mode, g.decision.mode);
    assert!(applied.proactive.enabled && applied.followup.enabled);
    assert_eq!(applied.reply_mention_sender, Some(false));
    assert_eq!(applied.decision.cooldown_secs, 180);
    assert_eq!(applied.decision.max_replies_per_hour, 5);
    assert!(!cfg.group_settings["30002"].followup.enabled);
    assert_eq!(
        settings::get(&f.app, &f.user, "feature_permissions")
            .await
            .unwrap(),
        Some(permission)
    );
    let raw = settings::get(&f.app, &f.user, "integrations")
        .await
        .unwrap()
        .unwrap()
        .to_string();
    assert!(!raw.contains("onebot-secret"));
}
#[tokio::test]
async fn stale_or_simultaneous_preset_saves_do_not_erase_channel_changes() {
    let f = Fixture::new().await;
    let original = f.view().await;
    f.seed(&json!({"search":{"url":"https://new.example.org","token":"new-secret"}}))
        .await;
    assert!(matches!(
        f.save("quiet", &original["revision"]).await,
        Err(lingxi::error::Error::Conflict(_))
    ));
    assert!(
        settings::get(&f.app, &f.user, "interaction")
            .await
            .unwrap()
            .is_none()
    );
    let current = f.view().await;
    let (a, b) = tokio::join!(
        f.save("quiet", &current["revision"]),
        f.save("lively", &current["revision"])
    );
    assert_eq!(usize::from(a.is_ok()) + usize::from(b.is_ok()), 1);
    let after = lingxi::integrations::config(&f.app, &f.user).await.unwrap();
    assert_eq!(after["search"]["token"], "new-secret");
}
#[tokio::test]
async fn manual_group_overrides_are_detected_and_preferences_are_account_scoped() {
    let f = Fixture::new().await;
    let original = f.view().await;
    f.save("quiet", &original["revision"]).await.unwrap();
    let mut config = lingxi::integrations::config(&f.app, &f.user).await.unwrap();
    config["qq"]["reply_style"]["max_messages"] = json!(4);
    f.seed(&config).await;
    assert_eq!(f.view().await["customized"], true);
    let other = auth::create_user(&f.app, "other", "another-long-password".into(), false)
        .await
        .unwrap();
    let second = interaction::view(State(f.app.clone()), other.clone())
        .await
        .unwrap()
        .0;
    assert_eq!(second["data"]["configured"], false);
    assert!(
        interaction::prompt(&f.app, &f.user)
            .await
            .unwrap()
            .contains("安静陪伴")
    );
    assert!(
        interaction::prompt(&f.app, &other)
            .await
            .unwrap()
            .contains("自然相处")
    );
}
#[tokio::test]
async fn normal_chat_uses_natural_style_without_changing_the_base_personality() {
    let f = Fixture::new().await;
    let conversation = lingxi::chat::create(&f.app, &f.user, "test", None)
        .await
        .unwrap();
    let profile = settings::Profile {
        id: "fish".into(),
        name: "鱼".into(),
        protocol: "openai".into(),
        base_url: "https://example.org/v1".into(),
        model: "mock".into(),
        api_key: String::new(),
        context_window: 32000,
        max_output: 4096,
        system_prompt: "蓝色大肥鱼，有点傲娇".into(),
    };
    let context =
        lingxi::chat::build_context(&f.app, &f.user, &conversation.id, &profile, "今天累了")
            .await
            .unwrap();
    let text = json!(context).to_string();
    assert!(text.contains("蓝色大肥鱼"));
    assert!(text.contains("不复述提问"));
    assert!(text.contains("认真提问再展开"));
    assert!(text.contains("不假装真人"));
}
#[test]
fn only_unambiguous_farewells_end_followups() {
    for text in [" 谢谢！ ", "晚安～", "先这样。", "OK", "不用回复了"] {
        assert!(interaction::closing_message(text), "{text}");
    }
    for text in [
        "谢谢，再帮我看个问题",
        "晚安是什么意思？",
        "他说‘先这样’",
        "好玩吗",
        "不聊了？",
        "今天好累",
    ] {
        assert!(!interaction::closing_message(text), "{text}");
    }
}
