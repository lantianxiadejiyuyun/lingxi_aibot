use crate::{
    auth::User,
    error::{Error, Result, invalid},
    state::App,
};
use serde_json::{Value, json};
use std::{
    path::{Component, Path, PathBuf},
    time::Duration,
};
use tokio_util::sync::CancellationToken;

pub async fn list_storage(app: &App, user: &User) -> Result<Value> {
    let cfg = crate::integrations::config(app, user).await?;
    Ok(json!(
        cfg["storage"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|v| json!({"id":v["id"],"name":v["name"]}))
            .collect::<Vec<_>>()
    ))
}
pub async fn storage_path(app: &App, user: &User, sid: &str, path: &str) -> Result<PathBuf> {
    let cfg = crate::integrations::config(app, user).await?;
    let storage = cfg["storage"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|s| s["id"] == sid)
        .ok_or(Error::NotFound)?;
    let root = storage["path"]
        .as_str()
        .ok_or_else(|| invalid("存储配置缺少路径"))?;
    let input = Path::new(path);
    if input
        .components()
        .any(|c| !matches!(c, Component::Normal(_) | Component::CurDir))
    {
        return Err(invalid("须使用存储内的相对路径"));
    }
    let root = tokio::fs::canonicalize(root)
        .await
        .map_err(|_| invalid("存储位置不可用"))?;
    let resolved = tokio::fs::canonicalize(root.join(input))
        .await
        .map_err(|_| Error::NotFound)?;
    if !resolved.starts_with(root) {
        return Err(Error::Forbidden);
    }
    Ok(resolved)
}
pub async fn list_files(app: &App, user: &User, sid: &str, path: &str) -> Result<Value> {
    let root = storage_path(app, user, sid, path).await?;
    let mut entries = tokio::fs::read_dir(&root)
        .await
        .map_err(|_| invalid("路径不是目录"))?;
    let mut files = Vec::new();
    while let Some(entry) = entries
        .next_entry()
        .await
        .map_err(|e| Error::Internal(e.into()))?
    {
        if files.len() >= 500 {
            break;
        }
        let ty = entry
            .file_type()
            .await
            .map_err(|e| Error::Internal(e.into()))?;
        // Do not expose or traverse symlinks in browser listings.
        if ty.is_symlink() {
            continue;
        }
        let metadata = entry
            .metadata()
            .await
            .map_err(|e| Error::Internal(e.into()))?;
        files.push(json!({"name":entry.file_name().to_string_lossy(),"directory":ty.is_dir(),"size":metadata.len()}));
    }
    Ok(json!({"items":files,"truncated":files.len()==500}))
}
async fn aria(app: &App, cfg: &Value, method: &str, params: Vec<Value>) -> Result<Value> {
    let url = cfg["url"]
        .as_str()
        .ok_or_else(|| invalid("请配置 aria2 RPC URL"))?;
    let mut args = Vec::new();
    if let Some(token) = cfg["token"].as_str().filter(|s| !s.is_empty()) {
        args.push(json!(format!("token:{token}")));
    }
    args.extend(params);
    let response = crate::network::client(app, url)
        .await?
        .post(url)
        .json(&json!({"jsonrpc":"2.0","id":"lingxi","method":method,"params":args}))
        .send()
        .await
        .map_err(|_| Error::Provider("aria2 连接失败".into()))?;
    let data = crate::integrations::json_response(response).await?;
    if data.get("error").is_some() {
        return Err(Error::Provider("aria2 操作失败".into()));
    }
    Ok(data["result"].clone())
}
pub async fn download(
    app: &App,
    user: &User,
    params: &Value,
    job_id: &str,
    cancel: &CancellationToken,
) -> Result<Value> {
    let cfg = crate::integrations::config(app, user).await?;
    let sid = params["storage_id"]
        .as_str()
        .ok_or_else(|| invalid("storage_id 必填"))?;
    let root = storage_path(app, user, sid, "").await?;
    let url = params["url"].as_str().ok_or_else(|| invalid("url 必填"))?;
    if url.len() > 8000 {
        return Err(invalid("下载地址过长"));
    }
    if !url.starts_with("magnet:?xt=urn:btih:") {
        crate::network::validate_url(app, url).await?;
    }
    let kind = params["downloader"].as_str().unwrap_or("aria2");
    if kind == "aria2" {
        let aria_cfg = &cfg["aria2"];
        let gid = aria(
            app,
            aria_cfg,
            "aria2.addUri",
            vec![
                json!([url]),
                json!({"dir":root.to_string_lossy(),"continue":"true","max-tries":"3"}),
            ],
        )
        .await?;
        sqlx::query("UPDATE jobs SET result=? WHERE id=?")
            .bind(json!({"external_id":gid,"downloader":"aria2"}).to_string())
            .bind(job_id)
            .execute(&app.pool)
            .await?;
        loop {
            tokio::select! {_=cancel.cancelled()=>{let _=aria(app,aria_cfg,"aria2.pause",vec![gid.clone()]).await;return Err(Error::Cancelled);},_=tokio::time::sleep(Duration::from_secs(5))=>()}
            let status = aria(
                app,
                aria_cfg,
                "aria2.tellStatus",
                vec![
                    gid.clone(),
                    json!([
                        "status",
                        "totalLength",
                        "completedLength",
                        "downloadSpeed",
                        "errorCode"
                    ]),
                ],
            )
            .await?;
            sqlx::query("UPDATE jobs SET result=? WHERE id=?")
                .bind(json!({"external_id":gid,"progress":status}).to_string())
                .bind(job_id)
                .execute(&app.pool)
                .await?;
            match status["status"].as_str() {
                Some("complete") => return Ok(json!({"external_id":gid,"status":"completed"})),
                Some("error" | "removed") => {
                    return Err(Error::Provider("下载器报告下载失败".into()));
                }
                _ => (),
            }
        }
    } else if kind == "qbittorrent" {
        let q = &cfg["qbittorrent"];
        let base = q["url"]
            .as_str()
            .ok_or_else(|| invalid("请配置 qBittorrent URL"))?
            .trim_end_matches('/');
        let client = crate::network::client(app, base).await?;
        let response = client
            .post(format!("{base}/api/v2/auth/login"))
            .form(&[
                ("username", q["username"].as_str().unwrap_or("")),
                ("password", q["password"].as_str().unwrap_or("")),
            ])
            .send()
            .await
            .map_err(|_| Error::Provider("qBittorrent 登录失败".into()))?;
        let cookie = response
            .headers()
            .get_all("set-cookie")
            .iter()
            .filter_map(|h| h.to_str().ok())
            .find_map(|h| {
                h.split(';')
                    .next()
                    .filter(|v| v.starts_with("SID="))
                    .map(String::from)
            })
            .ok_or_else(|| Error::Provider("qBittorrent 没有返回登录会话".into()))?;
        let tag = format!("lingxi-{job_id}");
        let response = client
            .post(format!("{base}/api/v2/torrents/add"))
            .header("cookie", &cookie)
            .form(&[
                ("urls", url.to_owned()),
                ("savepath", root.to_string_lossy().into_owned()),
                ("tags", tag.clone()),
            ])
            .send()
            .await
            .map_err(|_| Error::Provider("提交下载失败".into()))?;
        crate::integrations::bounded_body(response, 10000).await?;
        // A submitted torrent is observable by its unique tag; completion is polled below.
        loop {
            tokio::select! {_=cancel.cancelled()=>return Err(Error::Cancelled),_=tokio::time::sleep(Duration::from_secs(5))=>()}
            let response = client
                .get(format!("{base}/api/v2/torrents/info"))
                .header("cookie", &cookie)
                .query(&[("tag", &tag)])
                .send()
                .await
                .map_err(|_| Error::Provider("读取下载进度失败".into()))?;
            let values = crate::integrations::json_response(response).await?;
            let torrent = values
                .as_array()
                .and_then(|a| a.first())
                .ok_or_else(|| Error::Provider("下载器未找到刚提交的任务".into()))?;
            sqlx::query("UPDATE jobs SET result=? WHERE id=?").bind(json!({"hash":torrent["hash"],"progress":torrent["progress"],"state":torrent["state"]}).to_string()).bind(job_id).execute(&app.pool).await?;
            if torrent["progress"].as_f64().unwrap_or(0.0) >= 1.0 {
                return Ok(json!({"hash":torrent["hash"],"status":"completed"}));
            }
            if ["error", "missingFiles"].contains(&torrent["state"].as_str().unwrap_or("")) {
                return Err(Error::Provider("下载器报告文件错误".into()));
            }
        }
    } else {
        Err(invalid("下载器须为 aria2 或 qbittorrent"))
    }
}
