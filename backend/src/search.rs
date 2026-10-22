use crate::{
    auth::User,
    error::{Result, invalid},
    state::App,
};
use serde_json::{Value, json};
pub async fn search(app: &App, user: &User, query: &str) -> Result<Value> {
    let query = query.trim();
    if query.is_empty() || query.chars().count() > 200 {
        return Err(invalid("搜索词须为1–200字符"));
    }
    let mut results = Vec::new();
    for kind in ["notes", "pages", "bookmarks"] {
        let result = crate::records::list(
            app,
            user,
            kind,
            &crate::records::List {
                q: Some(query.into()),
                limit: Some(5),
                ..Default::default()
            },
        )
        .await?;
        for record in result["items"].as_array().into_iter().flatten() {
            results.push(json!({"kind":kind,"id":record["id"],"title":record["title"],"text":record["content"].as_str().unwrap_or("").chars().take(500).collect::<String>()}));
        }
    }
    let pattern = format!(
        "%{}%",
        query
            .replace('\\', "\\\\")
            .replace('%', "\\%")
            .replace('_', "\\_")
    );
    let messages:Vec<(String,i64,String)>=sqlx::query_as("SELECT c.id,m.id,m.content FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.user_id=? AND m.content LIKE ? ESCAPE '\\' ORDER BY m.id DESC LIMIT 5")
        .bind(&user.id).bind(&pattern).fetch_all(&app.pool).await?;
    for (cid, id, text) in messages {
        results.push(json!({"kind":"conversation","id":cid,"message_id":id,"text":text.chars().take(500).collect::<String>()}));
    }
    for memory in crate::memory::list(app, user).await? {
        if memory.content.contains(query) {
            results.push(json!({"kind":"memory","id":memory.id,"text":memory.content}));
        }
    }
    let cfg = crate::integrations::config(app, user).await?;
    let mut mode = "keyword";
    if cfg["embedding"]["url"]
        .as_str()
        .is_some_and(|s| !s.is_empty())
    {
        match semantic(app, user, &cfg["embedding"], query).await {
            Ok(items) => {
                mode = "hybrid";
                for item in items {
                    if !results
                        .iter()
                        .any(|r| r["id"] == item["id"] && r["kind"] == item["kind"])
                    {
                        results.push(item);
                    }
                }
            }
            Err(error) => {
                tracing::warn!(%error,"semantic search unavailable; keyword results retained")
            }
        }
    }
    Ok(json!({"mode":mode,"items":results}))
}

pub async fn embed(app: &App, cfg: &Value, text: &str) -> Result<Vec<f64>> {
    let url = format!(
        "{}/embeddings",
        cfg["url"]
            .as_str()
            .ok_or_else(|| invalid("缺少嵌入服务 URL"))?
            .trim_end_matches('/')
    );
    let response = crate::network::client(app, &url)
        .await?
        .post(url)
        .bearer_auth(cfg["api_key"].as_str().unwrap_or(""))
        .json(&json!({"model":cfg["model"],"input":text.chars().take(8000).collect::<String>()}))
        .send()
        .await
        .map_err(|_| crate::error::Error::Provider("嵌入服务请求失败".into()))?;
    let data = crate::integrations::json_response(response).await?;
    let vec: Vec<f64> = serde_json::from_value(data["data"][0]["embedding"].clone())
        .map_err(|_| invalid("嵌入向量无效"))?;
    if vec.is_empty() || vec.len() > 16384 || vec.iter().any(|x| !x.is_finite()) {
        return Err(invalid("嵌入向量维度或数值无效"));
    }
    let norm = vec.iter().map(|x| x * x).sum::<f64>().sqrt();
    if !norm.is_finite() || norm == 0.0 {
        return Err(invalid("嵌入向量范数无效"));
    }
    Ok(vec.into_iter().map(|x| x / norm).collect())
}
pub async fn index(app: &App, user: &User, kind: &str, rid: &str) -> Result<()> {
    if !["notes", "pages", "bookmarks"].contains(&kind) {
        return Ok(());
    }
    let cfg = crate::integrations::config(app, user).await?;
    let cfg = &cfg["embedding"];
    if !cfg["url"].as_str().is_some_and(|s| !s.is_empty()) {
        return Ok(());
    }
    let record = match crate::records::get(app, user, kind, rid).await {
        Ok(r) => r,
        Err(crate::error::Error::NotFound) => return Ok(()),
        Err(e) => return Err(e),
    };
    let text = format!("{}\n{}", record.title, record.content);
    let hash = crate::db::digest(&text);
    let vector = embed(app, cfg, &text).await?;
    // Discard an embedding if its source changed while the provider was running.
    sqlx::query("INSERT INTO embeddings(user_id,source_id,source_kind,model,content_hash,vector) SELECT ?,?,?,?,?,? WHERE EXISTS(SELECT 1 FROM records WHERE id=? AND user_id=? AND revision=? AND deleted_at IS NULL) ON CONFLICT(user_id,source_kind,source_id) DO UPDATE SET model=excluded.model,content_hash=excluded.content_hash,vector=excluded.vector")
        .bind(&user.id).bind(rid).bind(kind).bind(cfg["model"].as_str().unwrap_or("")).bind(hash).bind(json!(vector).to_string()).bind(rid).bind(&user.id).bind(record.revision).execute(&app.pool).await?;
    Ok(())
}
async fn semantic(app: &App, user: &User, cfg: &Value, query: &str) -> Result<Vec<Value>> {
    let query = embed(app, cfg, query).await?;
    type Row = (String, String, String, String, String, String);
    let rows:Vec<Row>=sqlx::query_as("SELECT e.source_id,e.source_kind,e.vector,e.content_hash,r.title,r.content FROM embeddings e JOIN records r ON r.id=e.source_id AND r.user_id=e.user_id WHERE e.user_id=? AND e.model=? AND r.deleted_at IS NULL ORDER BY r.updated_at DESC LIMIT 5000")
        .bind(&user.id).bind(cfg["model"].as_str().unwrap_or("")).fetch_all(&app.pool).await?;
    let mut scored = Vec::new();
    for (id, kind, vector, hash, title, content) in rows {
        if hash != crate::db::digest(&format!("{title}\n{content}")) {
            continue;
        }
        let Ok(vector) = serde_json::from_str::<Vec<f64>>(&vector) else {
            continue;
        };
        if vector.len() != query.len() {
            continue;
        }
        let score = vector.iter().zip(&query).map(|(a, b)| a * b).sum::<f64>();
        if !score.is_finite() {
            continue;
        }
        scored.push((score,json!({"id":id,"kind":kind,"title":title,"text":content.chars().take(500).collect::<String>(),"score":score})));
    }
    scored.sort_by(|a, b| b.0.total_cmp(&a.0));
    Ok(scored.into_iter().take(5).map(|(_, v)| v).collect())
}
