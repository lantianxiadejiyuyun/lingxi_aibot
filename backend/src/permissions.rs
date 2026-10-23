//! Account-owned feature permissions. Configuration and historical data remain editable.
use crate::{
    auth::User,
    error::{Result, invalid},
    settings,
    state::App,
};
use axum::{Json, extract::State};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Feature {
    Chat,
    Vision,
    StickerCollection,
    StickerReply,
    Memory,
    PersonaGrowth,
    Proactive,
    Bilibili,
    Tools,
}
#[derive(Clone, Copy)]
pub enum Scope {
    Private,
    Group,
}
impl Scope {
    pub fn for_group(group: Option<&str>) -> Self {
        if group.is_some() {
            Self::Group
        } else {
            Self::Private
        }
    }
}
pub const FEATURES: [Feature; 9] = [
    Feature::Chat,
    Feature::Vision,
    Feature::StickerCollection,
    Feature::StickerReply,
    Feature::Memory,
    Feature::PersonaGrowth,
    Feature::Proactive,
    Feature::Bilibili,
    Feature::Tools,
];
impl Feature {
    pub fn group_supported(self) -> bool {
        !matches!(self, Self::PersonaGrowth | Self::Bilibili | Self::Tools)
    }
    pub fn label(self) -> &'static str {
        match self {
            Self::Chat => "对话与回复",
            Self::Vision => "图片与 GIF 识别",
            Self::StickerCollection => "图片收藏",
            Self::StickerReply => "表情回复",
            Self::Memory => "长期记忆",
            Self::PersonaGrowth => "人设养成",
            Self::Proactive => "主动消息与延迟补充",
            Self::Bilibili => "B 站阅读",
            Self::Tools => "AI 工具操作",
        }
    }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Grant {
    pub enabled: bool,
    pub private: bool,
    pub group: bool,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub enabled: bool,
    pub features: BTreeMap<Feature, Grant>,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            enabled: true,
            features: FEATURES
                .into_iter()
                .map(|f| {
                    (
                        f,
                        Grant {
                            enabled: true,
                            private: true,
                            group: f.group_supported(),
                        },
                    )
                })
                .collect(),
        }
    }
}
impl Config {
    pub fn allows(&self, feature: Feature, scope: Scope) -> bool {
        self.enabled
            && self.features.get(&feature).is_some_and(|g| {
                g.enabled
                    && match scope {
                        Scope::Private => g.private,
                        Scope::Group => feature.group_supported() && g.group,
                    }
            })
    }
    fn validate(&self) -> Result<()> {
        if self.features.len() != FEATURES.len()
            || FEATURES.iter().any(|f| !self.features.contains_key(f))
        {
            return Err(invalid("请提交完整的功能权限设置"));
        }
        if self
            .features
            .iter()
            .any(|(f, g)| !f.group_supported() && g.group)
        {
            return Err(invalid("群聊不开放私人工具、B站书房和主人人设养成"));
        }
        Ok(())
    }
}
pub async fn config(app: &App, user: &User) -> Result<Config> {
    match settings::get(app, user, "feature_permissions").await? {
        None => Ok(Config::default()),
        Some(value) => serde_json::from_value(value).map_err(|_| invalid("功能权限配置无效")),
    }
}
pub async fn allowed(app: &App, user: &User, feature: Feature, scope: Scope) -> Result<bool> {
    Ok(config(app, user).await?.allows(feature, scope))
}
pub async fn require(app: &App, user: &User, feature: Feature, scope: Scope) -> Result<()> {
    if !allowed(app, user, feature, scope).await? {
        return Err(invalid(format!(
            "{}权限已关闭，请到设置中心调整",
            feature.label()
        )));
    }
    Ok(())
}
pub async fn view(State(app): State<App>, user: User) -> Result<Json<Value>> {
    Ok(Json(json!({"ok":true,"data":config(&app,&user).await?})))
}
pub async fn save(
    State(app): State<App>,
    user: User,
    Json(value): Json<Config>,
) -> Result<Json<Value>> {
    value.validate()?;
    settings::put(&app, &user, "feature_permissions", &json!(value)).await?;
    Ok(Json(json!({"ok":true,"data":value})))
}
