use crate::{
    auth::User,
    db::id,
    error::{Error, Result, invalid},
    state::App,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Profile {
    #[serde(default = "id")]
    pub id: String,
    pub name: String,
    pub protocol: String,
    pub base_url: String,
    pub model: String,
    #[serde(default)]
    pub api_key: String,
    #[serde(default = "window")]
    pub context_window: usize,
    #[serde(default = "output")]
    pub max_output: usize,
    #[serde(default)]
    pub system_prompt: String,
}
fn window() -> usize {
    128_000
}
fn output() -> usize {
    8192
}
pub async fn get(app: &App, user: &User, key: &str) -> Result<Option<Value>> {
    let value: Option<String> =
        sqlx::query_scalar("SELECT value FROM settings WHERE user_id=? AND key=?")
            .bind(&user.id)
            .bind(key)
            .fetch_optional(&app.pool)
            .await?;
    value
        .map(|v| serde_json::from_str(&v).map_err(|e| Error::Internal(e.into())))
        .transpose()
}
pub async fn put(app: &App, user: &User, key: &str, value: &Value) -> Result<()> {
    sqlx::query("INSERT INTO settings(user_id,key,value) VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value")
        .bind(&user.id).bind(key).bind(value.to_string()).execute(&app.pool).await?;
    Ok(())
}
pub async fn profiles(app: &App, user: &User, reveal: bool) -> Result<Vec<Profile>> {
    let mut profiles: Vec<Profile> =
        serde_json::from_value(get(app, user, "profiles").await?.unwrap_or(json!([])))
            .map_err(|e| Error::Internal(e.into()))?;
    for p in &mut profiles {
        if !p.api_key.is_empty() {
            p.api_key = if reveal {
                app.secrets.decrypt(&p.api_key)?
            } else {
                "********".into()
            };
        }
    }
    Ok(profiles)
}
pub async fn save_profiles(app: &App, user: &User, mut profiles: Vec<Profile>) -> Result<()> {
    if profiles.len() > 20 {
        return Err(invalid("最多保存 20 个配置"));
    }
    let old = profiles_raw(app, user).await?;
    let mut ids = std::collections::HashSet::new();
    for profile in &mut profiles {
        if profile.id.is_empty() || !ids.insert(profile.id.clone()) {
            return Err(invalid("配置 ID 为空或重复"));
        }
        if !["openai", "anthropic"].contains(&profile.protocol.as_str())
            || profile.model.trim().is_empty()
            || profile.name.trim().is_empty()
        {
            return Err(invalid("须填写配置名称、协议和模型"));
        }
        if profile.context_window < 2048
            || profile.context_window > 4_000_000
            || profile.max_output < 128
            || profile.max_output > 65536
            || profile.max_output + 1024 >= profile.context_window
        {
            return Err(invalid("上下文和输出预算无效"));
        }
        if profile.system_prompt.len() > 32000 || profile.api_key.len() > 4096 {
            return Err(invalid("配置内容过长"));
        }
        crate::network::validate_url(app, &profile.base_url).await?;
        profile.base_url = profile.base_url.trim_end_matches('/').to_owned();
        profile.api_key = if profile.api_key == "********" {
            old.iter()
                .find(|p| p.id == profile.id)
                .map(|p| p.api_key.clone())
                .ok_or_else(|| invalid("新配置须填写 API Key"))?
        } else {
            app.secrets.encrypt(&profile.api_key)?
        };
    }
    put(app, user, "profiles", &json!(profiles)).await
}
async fn profiles_raw(app: &App, user: &User) -> Result<Vec<Profile>> {
    serde_json::from_value(get(app, user, "profiles").await?.unwrap_or(json!([])))
        .map_err(|e| Error::Internal(e.into()))
}
pub async fn active_profile(app: &App, user: &User, profile_id: Option<&str>) -> Result<Profile> {
    let profiles = profiles(app, user, true).await?;
    if let Some(id) = profile_id {
        return profiles
            .into_iter()
            .find(|p| p.id == id)
            .ok_or_else(|| invalid("会话的模型配置已删除，请重新选择"));
    }
    profiles
        .into_iter()
        .next()
        .ok_or_else(|| invalid("请先在设置中添加模型配置"))
}
