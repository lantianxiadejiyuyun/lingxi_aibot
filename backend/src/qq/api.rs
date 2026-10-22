use super::{config, numeric_id, store};
use crate::{
    auth::{self, User},
    db::{digest, now},
    error::{Error, Result, invalid},
    state::App,
};
use axum::{
    Json,
    extract::{Path, Query, State},
    http::HeaderMap,
};
use serde::Deserialize;
use serde_json::{Value, json};

fn ok(value: impl serde::Serialize) -> Json<Value> {
    Json(json!({"ok":true,"data":value}))
}
pub fn text_message(value: &Value, bot: &str) -> (String, bool) {
    if let Some(items) = value.as_array() {
        let text = items
            .iter()
            .filter(|i| i["type"] == "text")
            .filter_map(|i| i["data"]["text"].as_str())
            .collect::<Vec<_>>()
            .join("");
        let mentioned = items
            .iter()
            .any(|i| i["type"] == "at" && numeric_id(&i["data"]["qq"]).as_deref() == Some(bot));
        return (text, mentioned);
    }
    let raw = value.as_str().unwrap_or("");
    let mentioned = raw.contains(&format!("[CQ:at,qq={bot}]"));
    let mut text = String::new();
    let mut rest = raw;
    while let Some(start) = rest.find("[CQ:") {
        text.push_str(&rest[..start]);
        if let Some(end) = rest[start..].find(']') {
            rest = &rest[start + end + 1..];
        } else {
            rest = &rest[start..];
            break;
        }
    }
    text.push_str(rest);
    (
        text.replace("&#91;", "[")
            .replace("&#93;", "]")
            .replace("&#44;", ",")
            .replace("&amp;", "&"),
        mentioned,
    )
}
pub async fn inbound(
    State(app): State<App>,
    Path(uid): Path<String>,
    headers: HeaderMap,
    Json(value): Json<Value>,
) -> Result<Json<Value>> {
    let user = auth::user(&app, &uid)
        .await
        .map_err(|_| Error::Unauthorized)?;
    let cfg = config(&app, &user).await?;
    let supplied = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .unwrap_or("");
    let left = digest(supplied);
    let right = digest(&cfg.inbound.secret);
    let equal = left
        .bytes()
        .zip(right.bytes())
        .fold(0u8, |a, (l, r)| a | (l ^ r))
        == 0;
    if cfg.inbound.secret.len() < 32 || !equal {
        return Err(Error::Unauthorized);
    }
    Ok(ok(ingest(&app, &user, &value).await?))
}
pub async fn ingest(app: &App, user: &User, value: &Value) -> Result<Value> {
    let cfg = config(app, user).await?;
    if value["post_type"] == "message" && value["message_type"] == "private" {
        return crate::companion::private::receive_qq(app, user, value, &cfg).await;
    }
    if !cfg.enabled || value["post_type"] != "message" || value["message_type"] != "group" {
        return Ok(json!({"ignored":true}));
    }
    let group = numeric_id(&value["group_id"]).ok_or_else(|| invalid("group_id 无效"))?;
    let Some(cfg) = cfg.for_group(&group).filter(|c| c.enabled) else {
        return Ok(json!({"ignored":true}));
    };
    let qq = numeric_id(&value["user_id"]).ok_or_else(|| invalid("user_id 无效"))?;
    if !cfg.groups.contains(&group)
        || qq == cfg.bot_qq
        || numeric_id(&value["self_id"]).as_deref() != Some(&cfg.bot_qq)
    {
        return Ok(json!({"ignored":true}));
    }
    let message_id = value["message_id"]
        .as_i64()
        .map(|n| n.to_string())
        .or_else(|| {
            value["message_id"]
                .as_str()
                .filter(|s| s.len() <= 64 && !s.is_empty())
                .map(String::from)
        })
        .ok_or_else(|| invalid("message_id 必填"))?;
    let (content, mentioned) = text_message(&value["message"], &cfg.bot_qq);
    let content = content.trim().chars().take(4000).collect::<String>();
    let id = digest(&format!("{}:{}:{group}:{message_id}", user.id, cfg.bot_qq));
    let sticker_count = super::stickers::receive(
        app,
        user,
        &cfg,
        &id,
        &group,
        &qq,
        &content,
        &value["message"],
    )
    .await?;
    if content.is_empty() {
        return Ok(if sticker_count > 0 {
            json!({"accepted":true,"stickers_queued":sticker_count})
        } else {
            json!({"ignored":true})
        });
    }
    let owner = qq == cfg.owner_qq;
    let collecting = owner && cfg.stickers.enabled && super::stickers::owner_command(&content);
    let event = store::Event {
        id: id.clone(),
        user_id: user.id.clone(),
        group_id: group,
        qq_id: qq,
        name: value["sender"]["card"]
            .as_str()
            .filter(|s| !s.is_empty())
            .or_else(|| value["sender"]["nickname"].as_str())
            .unwrap_or("群成员")
            .chars()
            .take(80)
            .collect(),
        content,
        triggered: cfg.reply_mentioned && mentioned && !collecting,
        occurred_at: value["time"]
            .as_i64()
            .filter(|t| *t > 0 && *t <= now() + 300)
            .unwrap_or(now()),
        created_at: now(),
        reply: String::new(),
        delivery_status: "pending".into(),
    };
    store::insert(app, user, &event, owner).await?;
    super::sessions::touch(app, user, &event.group_id).await?;
    crate::jobs::enqueue(
        app,
        user,
        crate::jobs::Input {
            action: "qq_event".into(),
            params: json!({"id":id,"owner":owner}),
            run_at: None,
            request_id: Some(format!("qq-event:{id}")),
        },
    )
    .await?;
    Ok(json!({"accepted":true,"storage":if owner {"main"}else{"qq"},"event_id":id}))
}
#[derive(Deserialize)]
pub struct PersonQuery {
    pub group_id: String,
    pub qq_id: String,
    pub q: Option<String>,
}
pub async fn people(State(app): State<App>, user: User) -> Result<Json<Value>> {
    let cfg = config(&app, &user).await?;
    type Row = (String, String, String, i64, i64);
    let mut people = Vec::new();
    let rows: Vec<Row> = sqlx::query_as("SELECT group_id,qq_id,MAX(name),MAX(occurred_at),COUNT(*) FROM events WHERE user_id=? GROUP BY group_id,qq_id ORDER BY MAX(occurred_at) DESC LIMIT 300").bind(&user.id).fetch_all(store::pool(&app).await?).await?;
    for (group, qq, name, seen, messages) in rows {
        let count:i64 = sqlx::query_scalar("SELECT COUNT(*) FROM facts WHERE user_id=? AND group_id=? AND qq_id=? AND deleted_at IS NULL AND expires_at>?").bind(&user.id).bind(&group).bind(&qq).bind(now()).fetch_one(store::pool(&app).await?).await?;
        people.push(json!({"group_id":group,"qq_id":qq,"name":name,"last_seen":seen,"messages":messages,"memories":count,"storage":"qq"}));
    }
    let rows:Vec<Row> = sqlx::query_as("SELECT group_id,qq_id,MAX(name),MAX(occurred_at),COUNT(*) FROM qq_owner_events WHERE user_id=? GROUP BY group_id,qq_id ORDER BY MAX(occurred_at) DESC LIMIT 100").bind(&user.id).fetch_all(&app.pool).await?;
    for (group, qq, name, seen, messages) in rows {
        people.push(json!({"group_id":group,"qq_id":qq,"name":name,"last_seen":seen,"messages":messages,"storage":"main"}));
    }
    // Include manually created member memories even before receiving a group message.
    let rows:Vec<Row> = sqlx::query_as("SELECT group_id,qq_id,MAX(name),MAX(updated_at),COUNT(*) FROM facts WHERE user_id=? AND deleted_at IS NULL AND expires_at>? GROUP BY group_id,qq_id LIMIT 300").bind(&user.id).bind(now()).fetch_all(store::pool(&app).await?).await?;
    for (group, qq, name, seen, count) in rows {
        if !people
            .iter()
            .any(|p| p["group_id"] == group && p["qq_id"] == qq)
        {
            people.push(json!({"group_id":group,"qq_id":qq,"name":name,"last_seen":seen,"messages":0,"memories":count,"storage":"qq"}));
        }
    }
    let pending: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM facts WHERE user_id=? AND revision>indexed_revision",
    )
    .bind(&user.id)
    .fetch_one(store::pool(&app).await?)
    .await?;
    Ok(ok(
        json!({"items":people,"owner_qq":cfg.owner_qq,"enabled":cfg.enabled,"pending_vectors":pending,"collection":super::vector::collection(&user,&cfg)}),
    ))
}
pub async fn person(
    State(app): State<App>,
    user: User,
    Query(q): Query<PersonQuery>,
) -> Result<Json<Value>> {
    let cfg = config(&app, &user).await?;
    let owner = q.qq_id == cfg.owner_qq;
    let memories = if owner {
        let rows = crate::memory::list(&app, &user).await?;
        json!(
            rows.into_iter()
                .map(|memory| {
                    let evidence = memory.provenance["evidence"].clone();
                    let confidence = memory.provenance["confidence"].clone();
                    let mut value = serde_json::to_value(memory).unwrap();
                    value["evidence"] = evidence;
                    value["confidence"] = confidence;
                    value
                })
                .collect::<Vec<_>>()
        )
    } else if let Some(query) = q.q.filter(|s| !s.trim().is_empty()) {
        serde_json::to_value(
            super::vector::recall(&app, &user, &q.group_id, &q.qq_id, &query).await?,
        )
        .unwrap()
    } else {
        serde_json::to_value(store::facts(&app, &user, &q.group_id, &q.qq_id).await?).unwrap()
    };
    let history = store::history(
        &app,
        &user,
        &q.group_id,
        Some(&q.qq_id),
        owner,
        now() + 300,
        50,
    )
    .await?;
    Ok(ok(
        json!({"storage":if owner {"main"}else{"qq"},"memories":memories,"history":history}),
    ))
}
pub async fn remember(
    State(app): State<App>,
    user: User,
    Json(input): Json<store::FactInput>,
) -> Result<Json<Value>> {
    let id = store::save_fact(&app, &user, &input, "manual", 1.0, &json!([])).await?;
    let owner = input.qq_id == config(&app, &user).await?.owner_qq;
    Ok(ok(
        json!({"id":id,"storage":if owner {"main"}else{"qq"},"saved":true}),
    ))
}
pub async fn forget(
    State(app): State<App>,
    user: User,
    Path(id): Path<String>,
) -> Result<Json<Value>> {
    store::forget(&app, &user, &id).await?;
    Ok(ok(json!({"deleted":true,"vector_delete":"queued"})))
}
pub async fn reindex(State(app): State<App>, user: User) -> Result<Json<Value>> {
    let rows: Vec<String> = sqlx::query_scalar("SELECT id FROM facts WHERE user_id=? LIMIT 10000")
        .bind(&user.id)
        .fetch_all(store::pool(&app).await?)
        .await?;
    for id in &rows {
        crate::jobs::enqueue(
            &app,
            &user,
            crate::jobs::Input {
                action: "qq_index".into(),
                params: json!({"id":id}),
                run_at: None,
                request_id: None,
            },
        )
        .await?;
    }
    Ok(ok(json!({"queued":rows.len()})))
}
