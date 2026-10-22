use crate::{
    auth::User,
    db::id,
    error::{Error, Result, invalid},
    state::App,
};
use futures_util::StreamExt;
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

pub async fn config(app: &App, user: &User) -> Result<Value> {
    let Some(value) = crate::settings::get(app, user, "integrations").await? else {
        return Ok(json!({}));
    };
    let text = app
        .secrets
        .decrypt(value.as_str().ok_or_else(|| invalid("接入配置损坏"))?)?;
    serde_json::from_str(&text).map_err(|e| Error::Internal(e.into()))
}
pub fn redact(value: &mut Value) {
    if let Some(map) = value.as_object_mut() {
        for (key, value) in map {
            if ["api_key", "token", "password", "secret"].contains(&key.as_str())
                && value.as_str().is_some_and(|s| !s.is_empty())
            {
                *value = json!("********");
            } else {
                redact(value);
            }
        }
    } else if let Some(items) = value.as_array_mut() {
        for value in items {
            redact(value);
        }
    }
}
fn restore_mask(value: &mut Value, old: &Value) {
    match value {
        Value::Object(map) => {
            for (key, value) in map {
                if value == "********" {
                    *value = old[key].clone();
                } else {
                    restore_mask(value, &old[key]);
                }
            }
        }
        Value::Array(items) => {
            for (i, value) in items.iter_mut().enumerate() {
                restore_mask(value, &old[i]);
            }
        }
        _ => (),
    }
}
pub async fn save_config(app: &App, user: &User, mut value: Value) -> Result<()> {
    if !value.is_object() || value.to_string().len() > 100_000 {
        return Err(invalid("接入配置无效"));
    }
    let old = config(app, user).await?;
    restore_mask(&mut value, &old);
    crate::qq::validate_config(app, user, &value["qq"], &old["qq"]).await?;
    crate::companion::channels::validate(app, &value["channels"]).await?;
    if !user.is_admin && value["storage"] != old["storage"] {
        return Err(Error::Forbidden);
    }
    for key in [
        "search",
        "image",
        "embedding",
        "webhook",
        "aria2",
        "qbittorrent",
    ] {
        if let Some(url) = value[key]["url"].as_str().filter(|s| !s.is_empty()) {
            crate::network::validate_url(app, url).await?;
        }
    }
    if let Some(storage) = value.get("storage") {
        let items = storage
            .as_array()
            .ok_or_else(|| invalid("storage 须为数组"))?;
        if items.len() > 20 {
            return Err(invalid("最多20个存储位置"));
        }
        let mut seen = std::collections::HashSet::new();
        for item in items {
            let id = item["id"]
                .as_str()
                .filter(|s| !s.is_empty())
                .ok_or_else(|| invalid("存储 ID 必填"))?;
            let path = item["path"]
                .as_str()
                .ok_or_else(|| invalid("存储路径必填"))?;
            if !seen.insert(id) || !std::path::Path::new(path).is_absolute() {
                return Err(invalid("存储 ID 重复或路径非绝对路径"));
            }
        }
    }
    crate::settings::put(
        app,
        user,
        "integrations",
        &json!(app.secrets.encrypt(&value.to_string())?),
    )
    .await
}
pub async fn bounded_body(response: reqwest::Response, limit: usize) -> Result<Vec<u8>> {
    if !response.status().is_success() {
        return Err(Error::Provider(format!(
            "外部服务返回 HTTP {}",
            response.status().as_u16()
        )));
    }
    if response.content_length().is_some_and(|n| n > limit as u64) {
        return Err(Error::Provider("外部响应超过大小限制".into()));
    }
    let mut stream = response.bytes_stream();
    let mut bytes = Vec::new();
    while let Some(part) = stream.next().await {
        let part = part.map_err(|_| Error::Provider("读取外部响应失败".into()))?;
        if bytes.len() + part.len() > limit {
            return Err(Error::Provider("外部响应超过大小限制".into()));
        }
        bytes.extend(part);
    }
    Ok(bytes)
}
pub async fn json_response(response: reqwest::Response) -> Result<Value> {
    serde_json::from_slice(&bounded_body(response, 8_000_000).await?)
        .map_err(|_| Error::Provider("外部服务返回了无效 JSON".into()))
}
pub async fn fetch_url(app: &App, url: &str) -> Result<Value> {
    let client = crate::network::client(app, url).await?;
    let response = client
        .get(url)
        .send()
        .await
        .map_err(|_| Error::Provider("网页请求失败".into()))?;
    let bytes = bounded_body(response, 2_000_000).await?;
    let text = String::from_utf8_lossy(&bytes)
        .chars()
        .take(12000)
        .collect::<String>();
    Ok(json!({"url":url,"content":text,"untrusted":true}))
}
pub async fn web_search(app: &App, user: &User, query: &str) -> Result<Value> {
    if query.trim().is_empty() || query.len() > 1000 {
        return Err(invalid("搜索词无效"));
    }
    let cfg = config(app, user).await?;
    let base = cfg["search"]["url"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| invalid("请在接入设置配置 SearXNG 搜索 URL"))?;
    let url = format!("{}/search", base.trim_end_matches('/'));
    let client = crate::network::client(app, &url).await?;
    let response = client
        .get(url)
        .query(&[("q", query), ("format", "json")])
        .send()
        .await
        .map_err(|_| Error::Provider("搜索请求失败".into()))?;
    let data = json_response(response).await?;
    let results: Vec<Value> = data["results"]
        .as_array()
        .into_iter()
        .flatten()
        .take(8)
        .map(|r| json!({"title":r["title"],"url":r["url"],"content":r["content"]}))
        .collect();
    Ok(json!({"results":results}))
}
pub async fn generate_image(
    app: &App,
    user: &User,
    params: &Value,
    cancel: &CancellationToken,
) -> Result<Value> {
    let cfg = config(app, user).await?;
    let cfg = &cfg["image"];
    let prompt = params["prompt"]
        .as_str()
        .filter(|s| !s.trim().is_empty() && s.len() <= 16000)
        .ok_or_else(|| invalid("图片 prompt 无效"))?;
    let url = format!(
        "{}/images/generations",
        cfg["url"]
            .as_str()
            .ok_or_else(|| invalid("请配置图片接口"))?
            .trim_end_matches('/')
    );
    let client = crate::network::client(app, &url).await?;
    let response = tokio::select! {_=cancel.cancelled()=>return Err(Error::Cancelled),r=client.post(url)
    .bearer_auth(cfg["api_key"].as_str().unwrap_or(""))
    .json(&json!({"model":cfg["model"],"prompt":prompt,"n":1,"size":"1024x1024","response_format":"b64_json"})).send()=>r.map_err(|_|Error::Provider("图片请求失败".into()))?};
    let data = json_response(response).await?;
    let bytes = if let Some(text) = data["data"][0]["b64_json"].as_str() {
        use base64::Engine;
        base64::engine::general_purpose::STANDARD
            .decode(text)
            .map_err(|_| Error::Provider("图片编码无效".into()))?
    } else if let Some(url) = data["data"][0]["url"].as_str() {
        let client = crate::network::client(app, url).await?;
        let response = client
            .get(url)
            .send()
            .await
            .map_err(|_| Error::Provider("下载生成图片失败".into()))?;
        bounded_body(response, 16_000_000).await?
    } else {
        return Err(Error::Provider("图片接口没有返回图片".into()));
    };
    let ext = if bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        "png"
    } else if bytes.starts_with(&[0xff, 0xd8, 0xff]) {
        "jpg"
    } else if bytes.starts_with(b"RIFF") && bytes.get(8..12) == Some(b"WEBP") {
        "webp"
    } else {
        return Err(Error::Provider("只接受 PNG/JPEG/WebP 图片".into()));
    };
    let directory = app.data_dir.join("assets").join(&user.id);
    tokio::fs::create_dir_all(&directory)
        .await
        .map_err(|e| Error::Internal(e.into()))?;
    let file = format!("{}.{}", id(), ext);
    tokio::fs::write(directory.join(&file), bytes)
        .await
        .map_err(|e| Error::Internal(e.into()))?;
    let record = crate::records::create(
        app,
        user,
        "images",
        crate::records::Input {
            title: prompt.chars().take(80).collect(),
            content: prompt.into(),
            data: json!({"file":file}),
            status: "done".into(),
            due_at: None,
            revision: None,
        },
    )
    .await?;
    Ok(json!({"id":record.id,"url":format!("/api/v2/assets/{}",record.id)}))
}
pub async fn send_webhook(app: &App, user: &User, params: &Value) -> Result<Value> {
    let cfg = config(app, user).await?;
    let cfg = &cfg["webhook"];
    let url = cfg["url"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| invalid("请配置通知 Webhook"))?;
    let text = params["body"]
        .as_str()
        .ok_or_else(|| invalid("body 必填"))?;
    let body = match cfg["kind"].as_str().unwrap_or("generic") {
        "feishu" => json!({"msg_type":"text","content":{"text":text}}),
        "onebot" => json!({"user_id":cfg["recipient"],"message":text}),
        _ => json!({"title":params["title"],"body":text,"recipient":cfg["recipient"]}),
    };
    let client = crate::network::client(app, url).await?;
    let mut request = client.post(url).json(&body);
    if let Some(token) = cfg["token"].as_str().filter(|s| !s.is_empty()) {
        request = request.bearer_auth(token);
    }
    let response = request
        .send()
        .await
        .map_err(|_| Error::Provider("通知发送失败".into()))?;
    bounded_body(response, 1_000_000).await?;
    Ok(json!({"sent":true}))
}
