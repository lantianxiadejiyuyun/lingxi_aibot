//! Public service addresses are supplied by deployment, never inferred from Host headers.
use crate::{
    auth::User,
    error::{Error, Result, invalid},
    state::App,
};
use axum::{Json, extract::State};
use serde_json::{Value, json};

#[derive(Clone, Default)]
pub struct Config {
    pub public_url: Option<String>,
    pub napcat_webui_url: Option<String>,
    pub napcat_http_url: Option<String>,
    pub napcat_managed: bool,
    pub napcat_http_token: Option<String>,
}

fn address(value: &Option<String>, name: &str) -> anyhow::Result<Option<String>> {
    let Some(value) = value.as_deref().map(str::trim).filter(|v| !v.is_empty()) else {
        return Ok(None);
    };
    let url =
        url::Url::parse(value).map_err(|_| anyhow::anyhow!("{name} must be an HTTP(S) URL"))?;
    anyhow::ensure!(
        matches!(url.scheme(), "http" | "https")
            && url.host_str().is_some()
            && url.username().is_empty()
            && url.password().is_none()
            && url.query().is_none()
            && url.fragment().is_none(),
        "{name} must be an HTTP(S) URL without credentials, query or fragment"
    );
    Ok(Some(url.as_str().trim_end_matches('/').to_string()))
}

impl Config {
    pub fn from_cli(cli: &crate::config::Cli) -> anyhow::Result<Self> {
        let token = cli.napcat_http_token.clone().filter(|v| !v.is_empty());
        anyhow::ensure!(
            token
                .as_ref()
                .is_none_or(|v| (32..=256).contains(&v.len()) && !v.chars().any(char::is_control)),
            "LINGXI_NAPCAT_HTTP_TOKEN must contain 32–256 bytes without control characters"
        );
        Ok(Self {
            public_url: address(&cli.public_url, "LINGXI_PUBLIC_URL")?,
            napcat_webui_url: address(&cli.napcat_webui_url, "LINGXI_NAPCAT_WEBUI_URL")?,
            napcat_http_url: address(&cli.napcat_http_url, "LINGXI_NAPCAT_HTTP_URL")?,
            napcat_managed: cli.napcat_managed,
            napcat_http_token: token,
        })
    }
    fn configure_available(&self) -> bool {
        self.napcat_managed && self.napcat_http_url.is_some() && self.napcat_http_token.is_some()
    }
}

pub async fn view(State(app): State<App>, user: User) -> Result<Json<Value>> {
    if !user.is_admin {
        return Err(Error::Forbidden);
    }
    let cfg = &app.deployment;
    let mut relative = url::Url::parse("http://placeholder/api/v2/qq/inbound/").unwrap();
    relative
        .path_segments_mut()
        .unwrap()
        .pop_if_empty()
        .push(&user.id);
    let path = relative.path();
    let callback = cfg
        .public_url
        .as_ref()
        .map(|url| format!("{url}{path}"))
        .unwrap_or_else(|| path.to_string());
    Ok(Json(json!({"ok":true,"data":{
        "public_url":cfg.public_url,
        "napcat":{
            "managed":cfg.napcat_managed,
            "webui_url":cfg.napcat_webui_url,
            "http_url":cfg.napcat_http_url,
            "callback_url":callback,
            "internal_callback_url":cfg.napcat_managed.then(||format!("http://app:8000{path}")),
            "configure_available":cfg.configure_available()
        },
        "feishu":{"managed":app.feishu_embedded}
    }})))
}

/// Apply server-side deployment credentials only after the administrator chooses the managed service.
pub async fn configure_napcat(State(app): State<App>, user: User) -> Result<Json<Value>> {
    if !user.is_admin {
        return Err(Error::Forbidden);
    }
    let cfg = &app.deployment;
    if !cfg.configure_available() {
        return Err(invalid("部署未提供内置 NapCat 的连接配置"));
    }
    let mut integrations = crate::integrations::config(&app, &user).await?;
    let mut qq: crate::qq::Config =
        serde_json::from_value(integrations.get("qq").cloned().unwrap_or(json!({})))
            .map_err(|_| invalid("QQ 配置无效"))?;
    qq.onebot.url = cfg.napcat_http_url.clone().unwrap();
    qq.onebot.token = cfg.napcat_http_token.clone().unwrap();
    integrations["qq"] = serde_json::to_value(qq).map_err(|e| Error::Internal(e.into()))?;
    crate::integrations::save_config(&app, &user, integrations).await?;
    Ok(Json(json!({"ok":true,"data":{"configured":true}})))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn addresses_reject_embedded_secrets_and_active_content() {
        for raw in [
            "javascript:alert(1)",
            "http://user:secret@localhost/",
            "http://localhost/?token=abc",
            "http://localhost/#token",
        ] {
            assert!(address(&Some(raw.into()), "URL").is_err());
        }
        assert_eq!(
            address(&Some(" https://example.com/webui/ ".into()), "URL")
                .unwrap()
                .as_deref(),
            Some("https://example.com/webui")
        );
        assert!(address(&Some("".into()), "URL").unwrap().is_none());
    }
}
