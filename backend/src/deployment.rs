//! Public service addresses are supplied by deployment, never inferred from Host headers.
use crate::{
    auth::User,
    error::{Error, Result, invalid},
    state::App,
};
use axum::{Json, extract::State};
use serde_json::{Value, json};
use std::{path::PathBuf, time::Duration};
use tokio::io::AsyncReadExt;

#[derive(Clone, Default)]
pub struct Config {
    pub public_url: Option<String>,
    pub napcat_webui_url: Option<String>,
    pub napcat_http_url: Option<String>,
    pub napcat_managed: bool,
    pub napcat_http_token: Option<String>,
    pub napcat_config_dir: Option<PathBuf>,
    pub napcat_webui_internal_url: Option<String>,
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
            napcat_config_dir: cli
                .napcat_config_dir
                .clone()
                .filter(|p| !p.as_os_str().is_empty()),
            napcat_webui_internal_url: address(
                &cli.napcat_webui_internal_url,
                "LINGXI_NAPCAT_WEBUI_INTERNAL_URL",
            )?,
        })
    }
    fn configure_available(&self) -> bool {
        self.napcat_managed && self.napcat_http_url.is_some() && self.napcat_http_token.is_some()
    }

    /// The deployment supplies a read-only directory, never a request-controlled path.
    /// Read on each explicit login so NapCat key rotation does not leave a stale copy.
    async fn webui_key(&self) -> Option<String> {
        if !self.napcat_managed
            || self.napcat_webui_url.is_none()
            || self.napcat_webui_internal_url.is_none()
        {
            return None;
        }
        let path = self.napcat_config_dir.as_ref()?.join("webui.json");
        let metadata = tokio::fs::metadata(&path).await.ok()?;
        const LIMIT: u64 = 65_536;
        if !metadata.is_file() || metadata.len() > LIMIT {
            return None;
        }
        let mut bytes = Vec::new();
        tokio::fs::File::open(path)
            .await
            .ok()?
            .take(LIMIT + 1)
            .read_to_end(&mut bytes)
            .await
            .ok()?;
        if bytes.len() as u64 > LIMIT {
            return None;
        }
        let data: Value = serde_json::from_slice(&bytes).ok()?;
        let key = data["token"].as_str()?;
        (!key.trim().is_empty() && key.len() <= 1024 && !key.chars().any(char::is_control))
            .then(|| key.to_owned())
    }
}

pub async fn view(State(app): State<App>, user: User) -> Result<Json<Value>> {
    if !user.is_admin {
        return Err(Error::Forbidden);
    }
    let cfg = &app.deployment;
    let login_available = cfg.webui_key().await.is_some();
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
            "configure_available":cfg.configure_available(),
            "login_available":login_available
        },
        "feishu":{"managed":app.feishu_embedded}
    }})))
}

/// Authenticate server-to-server. The long-lived WebUI key never leaves the server.
/// Only the normal NapCat session credential is handed to its own origin by postMessage;
/// neither key nor credential is placed in any URL, discovery response, or log.
pub async fn napcat_login(State(app): State<App>, user: User) -> Result<Json<Value>> {
    if !user.is_admin {
        return Err(Error::Forbidden);
    }
    if !app.allow_login(&format!("napcat-webui:{}", user.id)) {
        return Err(Error::Busy);
    }
    let cfg = &app.deployment;
    let key = cfg.webui_key().await.ok_or_else(|| {
        invalid("暂时无法读取 NapCat 管理配置，请确认 NapCat 已启动且配置目录已只读挂载")
    })?;
    let public = address(&cfg.napcat_webui_url, "NapCat WebUI")
        .ok()
        .flatten()
        .ok_or_else(|| invalid("NapCat 管理地址未正确配置"))?;
    let internal = address(&cfg.napcat_webui_internal_url, "NapCat internal WebUI")
        .ok()
        .flatten()
        .ok_or_else(|| invalid("NapCat 内部管理地址未正确配置"))?;
    let endpoint = format!("{internal}/api/auth/login");
    let client = crate::network::local_client(&app, &endpoint)
        .await
        .map_err(|_| invalid("NapCat 内部管理地址必须是已允许访问的局域网地址"))?;
    let hash = crate::db::digest(&format!("{key}.napcat"));
    let response = client
        .post(&endpoint)
        .timeout(Duration::from_secs(10))
        .json(&json!({"hash":hash}))
        .send()
        .await
        .map_err(|_| Error::Provider("暂时无法连接 NapCat，请稍后重试".into()))?;
    let bytes = crate::integrations::bounded_body(response, 65_536)
        .await
        .map_err(|_| Error::Provider("NapCat 登录服务暂时不可用".into()))?;
    let body: Value = serde_json::from_slice(&bytes)
        .map_err(|_| Error::Provider("NapCat 返回了无效登录响应".into()))?;
    if body["data"]["require2FA"] == true {
        return Err(invalid("NapCat 已开启二次验证，请通过普通管理入口完成验证"));
    }
    if body["code"].as_i64() != Some(0) {
        return Err(Error::Provider(
            "NapCat 登录失败，请确认管理密钥与运行中的配置一致".into(),
        ));
    }
    let credential = body["data"]["Credential"]
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= 8192 && !s.chars().any(char::is_control))
        .ok_or_else(|| Error::Provider("NapCat 未返回有效登录会话".into()))?;
    let mut url = url::Url::parse(&public).map_err(|_| invalid("NapCat 管理地址无效"))?;
    if url.path().ends_with("/web_login") {
        url.path_segments_mut()
            .map_err(|_| invalid("NapCat 管理地址无效"))?
            .pop();
    }
    url.path_segments_mut()
        .map_err(|_| invalid("NapCat 管理地址无效"))?
        .pop_if_empty()
        .push("lingxi-login")
        .push("");
    Ok(Json(
        json!({"ok":true,"data":{"url":url.as_str(),"credential":credential}}),
    ))
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
