//! QQ group observations are isolated from the owner's personal assistant data.
pub mod api;
pub mod decision;
pub mod dialogue;
pub(crate) mod media;
pub mod outreach;
pub mod sessions;
pub(crate) mod sticker_cadence;
pub mod stickers;
pub mod store;
pub mod vector;
pub mod worker;

use crate::{
    auth::User,
    error::{Result, invalid},
    state::App,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

#[derive(Clone, Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct Endpoint {
    pub url: String,
    pub api_key: String,
    pub token: String,
    pub model: String,
}
#[derive(Clone, Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct Inbound {
    pub secret: String,
}
#[derive(Clone, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    pub enabled: bool,
    pub private_enabled: bool,
    pub owner_qq: String,
    pub bot_qq: String,
    pub groups: Vec<String>,
    pub group_settings: std::collections::BTreeMap<String, sessions::GroupSettings>,
    pub inbound: Inbound,
    pub onebot: Endpoint,
    pub vector: Endpoint,
    pub embedding: Endpoint,
    pub profile_id: Option<String>,
    pub collect_memory: bool,
    pub reply_mentioned: bool,
    pub reply_mention_sender: bool,
    pub retention_days: i64,
    pub stickers: stickers::Settings,
    pub decision: decision::Settings,
    pub reply_style: dialogue::Style,
    pub followup: dialogue::Followup,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            enabled: false,
            private_enabled: false,
            owner_qq: String::new(),
            bot_qq: String::new(),
            groups: vec![],
            group_settings: Default::default(),
            inbound: Inbound::default(),
            onebot: Endpoint::default(),
            vector: Endpoint::default(),
            embedding: Endpoint::default(),
            profile_id: None,
            collect_memory: true,
            reply_mentioned: true,
            reply_mention_sender: false,
            retention_days: 30,
            stickers: stickers::Settings::default(),
            decision: decision::Settings::default(),
            reply_style: dialogue::Style::default(),
            followup: dialogue::Followup::default(),
        }
    }
}
impl Config {
    pub fn for_group(&self, group: &str) -> Option<Self> {
        if !self.groups.iter().any(|g| g == group) {
            return None;
        }
        let mut cfg = self.clone();
        if let Some(settings) = self.group_settings.get(group) {
            cfg.enabled &= settings.enabled;
            cfg.profile_id = settings.profile_id.clone().or(cfg.profile_id);
            cfg.reply_mentioned = settings.reply_mentioned.unwrap_or(cfg.reply_mentioned);
            cfg.reply_mention_sender = settings
                .reply_mention_sender
                .unwrap_or(cfg.reply_mention_sender);
            cfg.collect_memory = settings.collect_memory.unwrap_or(cfg.collect_memory);
        }
        Some(cfg)
    }
}
pub async fn config(app: &App, user: &User) -> Result<Config> {
    let value = crate::integrations::config(app, user).await?;
    let mut cfg: Config = serde_json::from_value(value.get("qq").cloned().unwrap_or(json!({})))
        .map_err(|_| invalid("QQ 配置无效"))?;
    let access = crate::permissions::config(app, user).await?;
    cfg.enabled &= access.allows(
        crate::permissions::Feature::Chat,
        crate::permissions::Scope::Group,
    );
    cfg.private_enabled &= access.allows(
        crate::permissions::Feature::Chat,
        crate::permissions::Scope::Private,
    );
    Ok(cfg)
}
pub fn numeric_id(value: &Value) -> Option<String> {
    let id = match value {
        Value::String(s) => s.clone(),
        Value::Number(n) => n.as_u64()?.to_string(),
        _ => return None,
    };
    valid_id(&id).then_some(id)
}
pub fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 20
        && !id.starts_with('0')
        && id.bytes().all(|b| b.is_ascii_digit())
        && id.parse::<u64>().is_ok()
}
pub async fn validate_config(app: &App, user: &User, value: &Value, old: &Value) -> Result<()> {
    if value.is_null() {
        if !old["owner_qq"].is_null() {
            return Err(invalid("请停用 QQ 接入，保留已有身份归属配置"));
        }
        return Ok(());
    }
    let cfg: Config =
        serde_json::from_value(value.clone()).map_err(|_| invalid("QQ 配置字段无效"))?;
    cfg.stickers.validate()?;
    cfg.decision.validate()?;
    cfg.reply_style.validate()?;
    cfg.followup.validate()?;
    let unique: std::collections::HashSet<_> = cfg.groups.iter().collect();
    if unique.len() != cfg.groups.len() || cfg.groups.iter().any(|g| !valid_id(g)) {
        return Err(invalid("群号须为有效数字，不能重复添加同一个群"));
    }
    if cfg.group_settings.len() > 100 {
        return Err(invalid("群设置最多100项"));
    }
    for (group, settings) in &cfg.group_settings {
        if !cfg.groups.contains(group) {
            return Err(invalid("群设置须对应已添加的群号"));
        }
        settings.validate()?;
        if settings.decision.mode != decision::Mode::Off {
            crate::network::local_client(app, &cfg.decision.endpoint()?).await?;
            if settings.decision.mode == decision::Mode::Auto && settings.enabled && cfg.enabled {
                if cfg.onebot.url.is_empty() {
                    return Err(invalid("自动接话需要 OneBot HTTP 地址"));
                }
                crate::settings::active_profile(
                    app,
                    user,
                    settings.profile_id.as_deref().or(cfg.profile_id.as_deref()),
                )
                .await?;
            }
        }
        if let Some(profile) = &settings.profile_id {
            crate::settings::active_profile(app, user, Some(profile)).await?;
        }
    }
    if !(1..=365).contains(&cfg.retention_days) || cfg.groups.len() > 100 {
        return Err(invalid("群数最多100，原始群消息保留1–365天"));
    }
    if let Some(owner) = old["owner_qq"].as_str().filter(|s| !s.is_empty()) {
        if owner != cfg.owner_qq {
            return Err(invalid(
                "主人身份已绑定，不能清空或直接改号以免混合人物记忆；需先迁移所属数据",
            ));
        }
    }
    if !cfg.enabled && !cfg.private_enabled && cfg.owner_qq.is_empty() {
        return Ok(());
    }
    if !valid_id(&cfg.owner_qq)
        || !valid_id(&cfg.bot_qq)
        || cfg.owner_qq == cfg.bot_qq
        || cfg.groups.iter().any(|g| !valid_id(g))
    {
        return Err(invalid(
            "主人 QQ、机器人 QQ 和群号须是不同角色的有效数字 ID",
        ));
    }
    if cfg.enabled
        && (cfg.groups.is_empty()
            || cfg.inbound.secret.len() < 32
            || cfg.inbound.secret.len() > 256)
    {
        return Err(invalid("启用前请配置群白名单和至少32字符的回调密钥"));
    }
    if cfg.private_enabled {
        if cfg.onebot.url.is_empty() || !(32..=256).contains(&cfg.inbound.secret.len()) {
            return Err(invalid("个人 QQ 需要 OneBot 地址和32–256字符回调密钥"));
        }
        crate::settings::active_profile(app, user, cfg.profile_id.as_deref()).await?;
        if cfg.stickers.profile_id.is_some() {
            crate::settings::active_profile(app, user, cfg.stickers.profile_id.as_deref()).await?;
        }
    }
    for endpoint in [&cfg.onebot, &cfg.vector, &cfg.embedding] {
        if !endpoint.url.is_empty() {
            crate::network::validate_url(app, &endpoint.url).await?;
        }
    }
    let active: Vec<_> = cfg
        .groups
        .iter()
        .filter_map(|g| cfg.for_group(g))
        .filter(|c| c.enabled)
        .collect();
    if active.iter().any(|c| c.reply_mentioned) && cfg.onebot.url.is_empty() {
        return Err(invalid("回复群消息前请配置 OneBot HTTP 地址"));
    }
    if active.iter().any(|c| c.collect_memory)
        && (cfg.vector.url.is_empty()
            || cfg.embedding.url.is_empty()
            || cfg.embedding.model.is_empty())
    {
        return Err(invalid("人物记忆需要独立 Qdrant 地址及嵌入模型配置"));
    }
    for group in &active {
        if group.collect_memory
            || group.reply_mentioned
            || group
                .group_settings
                .values()
                .any(|s| s.proactive.enabled || s.followup.enabled)
        {
            crate::settings::active_profile(app, user, group.profile_id.as_deref()).await?;
        }
    }
    if active.iter().any(|g| {
        g.group_settings
            .values()
            .any(|s| s.proactive.enabled || s.followup.enabled)
    }) && cfg.onebot.url.is_empty()
    {
        return Err(invalid("主动会话需要 OneBot HTTP 地址"));
    }
    if (cfg.enabled || cfg.private_enabled) && cfg.stickers.enabled && cfg.stickers.auto_review {
        crate::settings::active_profile(
            app,
            user,
            cfg.stickers
                .profile_id
                .as_deref()
                .or(cfg.profile_id.as_deref()),
        )
        .await?;
    }
    Ok(())
}
