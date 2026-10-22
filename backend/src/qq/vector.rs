use super::{Config, store};
use crate::{
    auth::User,
    db::{digest, now},
    error::{Error, Result, invalid},
    state::App,
};
use serde_json::{Value, json};

pub fn collection(user: &User, cfg: &Config) -> String {
    // Changing embedding model/endpoint creates a new namespace, never mixes dimensions.
    format!(
        "lingxi_qq_{}",
        &digest(&format!(
            "{}:{}:{}",
            user.id, cfg.embedding.url, cfg.embedding.model
        ))[..24]
    )
}
async fn request(
    app: &App,
    cfg: &Config,
    method: reqwest::Method,
    path: &str,
    body: Option<&Value>,
) -> Result<reqwest::Response> {
    if cfg.vector.url.is_empty() {
        return Err(invalid("尚未配置 QQ 专用向量库"));
    }
    let url = format!("{}{path}", cfg.vector.url.trim_end_matches('/'));
    let mut request = crate::network::client(app, &url)
        .await?
        .request(method, &url);
    if !cfg.vector.api_key.is_empty() {
        request = request.header("api-key", &cfg.vector.api_key);
    }
    if let Some(body) = body {
        request = request.json(body);
    }
    request
        .send()
        .await
        .map_err(|_| Error::Provider("QQ 向量库连接失败".into()))
}
async fn ensure(app: &App, cfg: &Config, name: &str, size: usize) -> Result<()> {
    let path = format!("/collections/{name}");
    let response = request(app, cfg, reqwest::Method::GET, &path, None).await?;
    if response.status() == reqwest::StatusCode::NOT_FOUND {
        let response = request(
            app,
            cfg,
            reqwest::Method::PUT,
            &path,
            Some(&json!({"vectors":{"size":size,"distance":"Cosine"}})),
        )
        .await?;
        if response.status().is_success() {
            crate::integrations::json_response(response).await?;
            return Ok(());
        }
        if response.status() != reqwest::StatusCode::CONFLICT {
            crate::integrations::json_response(response).await?;
        }
    } else {
        let value = crate::integrations::json_response(response).await?;
        if value["result"]["config"]["params"]["vectors"]["size"].as_u64() != Some(size as u64) {
            return Err(invalid("QQ 向量库维度与嵌入模型不一致，请重建索引"));
        }
    }
    Ok(())
}
pub fn scope_filter(user: &User, group: &str, qq: &str) -> Value {
    json!({"must":[{"key":"user_id","match":{"value":user.id}},{"key":"group_id","match":{"value":group}},{"key":"qq_id","match":{"value":qq}},{"key":"expires_at","range":{"gt":now()}}]})
}
pub async fn index(app: &App, user: &User, fid: &str) -> Result<()> {
    let cfg = super::config(app, user).await?;
    let fact = store::fact(app, user, fid).await?;
    if fact.qq_id == cfg.owner_qq {
        return Err(invalid("主人记忆禁止写入群成员向量库"));
    }
    let name = collection(user, &cfg);
    if fact.deleted_at.is_some() || fact.expires_at <= now() {
        let response = request(
            app,
            &cfg,
            reqwest::Method::POST,
            &format!("/collections/{name}/points/delete?wait=true"),
            Some(&json!({"points":[fact.id]})),
        )
        .await?;
        if response.status() != reqwest::StatusCode::NOT_FOUND {
            crate::integrations::json_response(response).await?;
        }
    } else {
        let vector = crate::search::embed(
            app,
            &serde_json::to_value(&cfg.embedding).unwrap(),
            &fact.content,
        )
        .await?;
        ensure(app, &cfg, &name, vector.len()).await?;
        let current = store::fact(app, user, fid).await?;
        if current.revision != fact.revision {
            return Ok(());
        }
        let response = request(app,&cfg,reqwest::Method::PUT,&format!("/collections/{name}/points?wait=true"),Some(&json!({"points":[{"id":fact.id,"vector":vector,"payload":{"user_id":user.id,"group_id":fact.group_id,"qq_id":fact.qq_id,"content":fact.content,"revision":fact.revision,"expires_at":fact.expires_at}}]}))).await?;
        crate::integrations::json_response(response).await?;
    }
    let current = store::fact(app, user, fid).await?;
    if current.revision != fact.revision {
        // A slower stale write can arrive after a newer one; force a repair, even
        // if the latest revision's original job already completed.
        sqlx::query("UPDATE facts SET indexed_revision=0 WHERE id=? AND user_id=?")
            .bind(fid)
            .bind(&user.id)
            .execute(store::pool(app).await?)
            .await?;
        crate::jobs::enqueue(
            app,
            user,
            crate::jobs::Input {
                action: "qq_index".into(),
                params: json!({"id":fid}),
                run_at: None,
                request_id: None,
            },
        )
        .await?;
        return Ok(());
    }
    sqlx::query("UPDATE facts SET indexed_revision=? WHERE id=? AND user_id=? AND revision=?")
        .bind(fact.revision)
        .bind(fid)
        .bind(&user.id)
        .bind(fact.revision)
        .execute(store::pool(app).await?)
        .await?;
    Ok(())
}
pub async fn recall(
    app: &App,
    user: &User,
    group: &str,
    qq: &str,
    query: &str,
) -> Result<Vec<store::Fact>> {
    let cfg = super::config(app, user).await?;
    if qq == cfg.owner_qq {
        return Ok(vec![]);
    }
    let vector =
        crate::search::embed(app, &serde_json::to_value(&cfg.embedding).unwrap(), query).await?;
    let response = request(app,&cfg,reqwest::Method::POST,&format!("/collections/{}/points/query",collection(user,&cfg)),Some(&json!({"query":vector,"filter":scope_filter(user,group,qq),"limit":8,"with_payload":true}))).await?;
    if response.status() == reqwest::StatusCode::NOT_FOUND {
        return Ok(vec![]);
    }
    let data = crate::integrations::json_response(response).await?;
    let mut output = Vec::new();
    for point in data["result"]["points"].as_array().into_iter().flatten() {
        let Some(fid) = point["id"].as_str() else {
            continue;
        };
        // Recheck the authoritative store, including deletion/revision, even if a vector service misfilters.
        if let Ok(fact) = store::fact(app, user, fid).await {
            if fact.group_id == group
                && fact.qq_id == qq
                && fact.deleted_at.is_none()
                && fact.expires_at > now()
                && point["payload"]["revision"].as_i64() == Some(fact.revision)
            {
                output.push(fact);
            }
        }
    }
    Ok(output)
}
