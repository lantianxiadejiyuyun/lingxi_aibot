use crate::{
    db::{digest, now},
    error::{Error, Result, invalid},
    state::App,
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::HashMap;

#[derive(Deserialize)]
pub struct Snapshot {
    pub format: String,
    pub tables: HashMap<String, Vec<Value>>,
}
fn rows<'a>(snapshot: &'a Snapshot, table: &str) -> &'a [Value] {
    snapshot.tables.get(table).map(Vec::as_slice).unwrap_or(&[])
}
fn text<'a>(row: &'a Value, key: &str) -> &'a str {
    row[key].as_str().unwrap_or("")
}
fn legacy_id(kind: &str, value: &Value) -> String {
    format!(
        "legacy-{kind}-{}",
        value
            .as_i64()
            .map(|v| v.to_string())
            .unwrap_or_else(|| value.as_str().unwrap_or("unknown").into())
    )
}
fn time(value: &Value) -> Option<i64> {
    if let Some(n) = value.as_i64() {
        return Some(n);
    }
    let text = value.as_str()?;
    chrono::DateTime::parse_from_rfc3339(text)
        .map(|d| d.timestamp())
        .ok()
        .or_else(|| {
            chrono::NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%S%.f")
                .map(|d| d.and_utc().timestamp())
                .ok()
        })
}
/// One transaction, empty destination only; every original row is preserved encrypted.
pub async fn import(app: &App, bytes: &[u8], password: String) -> Result<Value> {
    let snapshot: Snapshot =
        serde_json::from_slice(bytes).map_err(|_| invalid("导出文件 JSON 无效"))?;
    if snapshot.format != "lingxi-legacy-v1" {
        return Err(invalid("不支持的导出格式"));
    }
    let source = digest(std::str::from_utf8(bytes).map_err(|_| invalid("导出文件须为 UTF-8"))?);
    let users = rows(&snapshot, "users");
    let admin = users
        .iter()
        .find(|u| u["is_admin"].as_bool() == Some(true) || u["is_admin"] == 1)
        .ok_or_else(|| invalid("旧库须含管理员"))?;
    let admin_id = legacy_id("user", &admin["id"]);
    let hash = crate::auth::hash_password(app, password).await?;
    let mut tx = app.pool.begin().await?;
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM users")
        .fetch_one(&mut *tx)
        .await?;
    if count != 0 {
        return Err(Error::Conflict(
            "导入目标须为空库；已有账户时拒绝导入".into(),
        ));
    }
    let mut archived = 0;
    let mut imported = 0;
    for (table, values) in &snapshot.tables {
        for (index, row) in values.iter().enumerate() {
            sqlx::query(
                "INSERT INTO legacy_archive(source,table_name,row_id,ciphertext) VALUES(?,?,?,?)",
            )
            .bind(&source)
            .bind(table)
            .bind(index.to_string())
            .bind(app.secrets.encrypt(&row.to_string())?)
            .execute(&mut *tx)
            .await?;
            archived += 1;
        }
    }
    for user in users {
        let uid = legacy_id("user", &user["id"]);
        let admin = user["is_admin"].as_bool() == Some(true) || user["is_admin"] == 1;
        // Only the first administrator is enabled. Reset other users individually.
        sqlx::query("INSERT INTO users(id,username,password_hash,timezone,is_admin,disabled,created_at) VALUES(?,?,?,?,?,?,?)")
            .bind(&uid).bind(text(user,"username")).bind(&hash).bind(if text(user,"timezone").is_empty(){"Asia/Shanghai"}else{text(user,"timezone")})
            .bind(admin).bind(uid!=admin_id).bind(time(&user["created_at"]).unwrap_or_else(now)).execute(&mut *tx).await?;
    }
    let owner = |row: &Value| {
        if row["user_id"].is_null() {
            admin_id.clone()
        } else {
            legacy_id("user", &row["user_id"])
        }
    };
    for (table, kind) in [
        ("tasks", "tasks"),
        ("notes", "notes"),
        ("events", "events"),
        ("expense_records", "expenses"),
        ("fitness_records", "fitness"),
        ("trip_plans", "trips"),
        ("webpages", "pages"),
        ("skills", "skills"),
        ("image_assets", "images"),
    ] {
        for row in rows(&snapshot, table) {
            let mut data = row.clone();
            data.as_object_mut()
                .ok_or_else(|| invalid("导出行须为对象"))?
                .remove("user_id");
            let (title, content, due) = match kind {
                "tasks" => (
                    text(row, "title").to_owned(),
                    text(row, "notes").to_owned(),
                    time(&row["due_utc"]),
                ),
                "events" => {
                    let start = time(&row["start_utc"]);
                    data["start_at"] = json!(start);
                    data["end_at"] = json!(time(&row["end_utc"]).or(start.map(|n| n + 3600)));
                    (
                        text(row, "title").into(),
                        text(row, "description").into(),
                        start,
                    )
                }
                "expenses" => {
                    let amount = row["amount"].as_f64().unwrap_or(0.0);
                    data["amount_cents"] = json!((amount * 100.0).round() as i64);
                    (
                        format!("{} · {}", text(row, "category"), text(row, "date")),
                        text(row, "notes").into(),
                        None,
                    )
                }
                "fitness" => {
                    data["minutes"] = row["duration_min"].clone();
                    (
                        format!("{} · {}", text(row, "workout_type"), text(row, "date")),
                        text(row, "notes").into(),
                        None,
                    )
                }
                "trips" => (
                    text(row, "destination").into(),
                    format!("{}\n{}", text(row, "notes"), text(row, "itinerary")),
                    None,
                ),
                "pages" => {
                    data["public"] = json!(false);
                    (text(row, "title").into(), text(row, "content").into(), None)
                }
                "skills" => {
                    data["enabled"] = json!(false);
                    data["legacy_code"] = row["code"].clone();
                    (
                        text(row, "name").into(),
                        text(row, "description").into(),
                        None,
                    )
                }
                "images" => {
                    data["legacy_file_path"] = row["file_path"].clone();
                    ("已导入图片元数据".into(), text(row, "prompt").into(), None)
                }
                _ => (text(row, "title").into(), text(row, "content").into(), None),
            };
            let status = if kind == "tasks" {
                text(row, "status")
            } else {
                "open"
            };
            sqlx::query("INSERT INTO records(id,user_id,kind,title,content,data,status,due_at,created_at,updated_at,deleted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)")
                .bind(legacy_id(kind,&row["id"])).bind(owner(row)).bind(kind).bind(title).bind(content).bind(data.to_string()).bind(status).bind(due)
                .bind(time(&row["created_at"]).unwrap_or_else(now)).bind(time(&row["updated_at"]).unwrap_or_else(now)).bind(time(&row["deleted_at"])).execute(&mut *tx).await?;
            imported += 1;
        }
    }
    for row in rows(&snapshot, "conversations") {
        let key = format!("chat_context:{}", row["id"]);
        let boundary = rows(&snapshot, "settings")
            .iter()
            .find(|r| r["key"] == key && r["user_id"] == row["user_id"])
            .and_then(|r| r["value"]["through_id"].as_i64())
            .unwrap_or(0);
        sqlx::query("INSERT INTO conversations(id,user_id,title,summary,through_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)")
            .bind(legacy_id("conversation",&row["id"])).bind(owner(row)).bind(text(row,"title")).bind(text(row,"summary")).bind(boundary)
            .bind(time(&row["created_at"]).unwrap_or_else(now)).bind(time(&row["updated_at"]).unwrap_or_else(now)).execute(&mut *tx).await?;
        imported += 1;
    }
    for row in rows(&snapshot, "messages") {
        sqlx::query("INSERT INTO messages(id,conversation_id,role,content,tool_calls,created_at) VALUES(?,?,?,?,?,?)")
            .bind(row["id"].as_i64().ok_or_else(||invalid("消息 ID 无效"))?).bind(legacy_id("conversation",&row["conversation_id"]))
            .bind(text(row,"role")).bind(text(row,"content")).bind(if row["tool_calls"].is_null(){None}else{Some(row["tool_calls"].to_string())})
            .bind(time(&row["created_at"]).unwrap_or_else(now)).execute(&mut *tx).await?;
        imported += 1;
    }
    for row in rows(&snapshot, "memories") {
        let content = text(row, "content");
        let key = digest(
            &content
                .split_whitespace()
                .collect::<String>()
                .to_lowercase(),
        );
        sqlx::query("INSERT INTO memories(id,user_id,fact_key,content,source,importance,expires_at,created_at,updated_at,deleted_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,fact_key) DO NOTHING")
            .bind(legacy_id("memory",&row["id"])).bind(owner(row)).bind(key).bind(content).bind(if text(row,"source")=="manual"{"manual"}else{"auto"})
            .bind(row["importance"].as_i64().unwrap_or(3).clamp(1,5)).bind(time(&row["expires_at"])).bind(time(&row["created_at"]).unwrap_or_else(now))
            .bind(time(&row["updated_at"]).unwrap_or_else(now)).bind(time(&row["deleted_at"])).execute(&mut *tx).await?;
        imported += 1;
    }
    for row in rows(&snapshot, "notifications") {
        sqlx::query("INSERT INTO notifications(id,user_id,title,body,read_at,created_at) VALUES(?,?,?,?,?,?)")
            .bind(legacy_id("notification",&row["id"])).bind(owner(row)).bind(text(row,"title")).bind(text(row,"body"))
            .bind(if row["read"].as_bool()==Some(true){Some(now())}else{None}).bind(time(&row["created_at"]).unwrap_or_else(now)).execute(&mut *tx).await?;
        imported += 1;
    }
    sqlx::query("INSERT INTO import_log(source,imported_at) VALUES(?,?)")
        .bind(&source)
        .bind(now())
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(
        json!({"users":users.len(),"imported_rows":imported,"archived_rows":archived,"enabled_admin":text(admin,"username"),"source_hash":source,
        "requires_configuration":["model profiles","integration credentials","schedules","image files","declarative skills"]}),
    )
}
