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
    Mood,
    PersonaGrowth,
    Proactive,
    Bilibili,
    WebSearch,
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
pub const FEATURES: [Feature; 11] = [
    Feature::Chat,
    Feature::Vision,
    Feature::StickerCollection,
    Feature::StickerReply,
    Feature::Memory,
    Feature::Mood,
    Feature::PersonaGrowth,
    Feature::Proactive,
    Feature::Bilibili,
    Feature::WebSearch,
    Feature::Tools,
];
impl Feature {
    pub fn group_default(self) -> bool {
        !matches!(
            self,
            Self::Mood | Self::PersonaGrowth | Self::Bilibili | Self::Tools | Self::WebSearch
        )
    }
    pub fn label(self) -> &'static str {
        match self {
            Self::Chat => "对话与回复",
            Self::Vision => "图片与 GIF 识别",
            Self::StickerCollection => "图片收藏",
            Self::StickerReply => "表情回复",
            Self::Memory => "长期记忆",
            Self::Mood => "持续心情",
            Self::PersonaGrowth => "人设养成",
            Self::Proactive => "主动消息与延迟补充",
            Self::Bilibili => "B 站阅读",
            Self::WebSearch => "联网搜索",
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
                            enabled: f != Feature::Mood,
                            private: true,
                            group: f.group_default(),
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
                        Scope::Group => g.group,
                    }
            })
    }
    fn validate(&self) -> Result<()> {
        if self.features.len() != FEATURES.len()
            || FEATURES.iter().any(|f| !self.features.contains_key(f))
        {
            return Err(invalid("请提交完整的功能权限设置"));
        }
        Ok(())
    }
}
pub async fn config(app: &App, user: &User) -> Result<Config> {
    match settings::get(app, user, "feature_permissions").await? {
        None => Ok(Config::default()),
        Some(value) => {
            let mut config: Config =
                serde_json::from_value(value).map_err(|_| invalid("功能权限配置无效"))?;
            // Legacy search was gated by personal tools. Preserve its effective access;
            // newly supported group capabilities remain opt-in.
            if !config.features.contains_key(&Feature::WebSearch) {
                let mut grant = config
                    .features
                    .get(&Feature::Tools)
                    .cloned()
                    .unwrap_or(Grant {
                        enabled: false,
                        private: false,
                        group: false,
                    });
                grant.group = false;
                config.features.insert(Feature::WebSearch, grant);
            }
            config.features.entry(Feature::Mood).or_insert(Grant {
                enabled: false,
                private: true,
                group: false,
            });
            config.validate()?;
            Ok(config)
        }
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
