use crate::{
    auth::User,
    db::{id, now},
    error::{Error, Result, invalid},
    state::App,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::{FromRow, QueryBuilder, Sqlite};

pub const KINDS: &[&str] = &[
    "tasks",
    "notes",
    "events",
    "expenses",
    "fitness",
    "trips",
    "pages",
    "skills",
    "images",
    "bookmarks",
];
pub fn check_kind(kind: &str) -> Result<()> {
    if KINDS.contains(&kind) {
        Ok(())
    } else {
        Err(Error::NotFound)
    }
}
#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(deny_unknown_fields)]
pub struct Input {
    pub title: String,
    #[serde(default)]
    pub content: String,
    #[serde(default = "empty_object")]
    pub data: Value,
    #[serde(default = "open")]
    pub status: String,
    pub due_at: Option<i64>,
    pub revision: Option<i64>,
}
fn empty_object() -> Value {
    json!({})
}
fn open() -> String {
    "open".into()
}
#[derive(Debug, Serialize, Deserialize, FromRow, Clone)]
pub struct Record {
    pub id: String,
    pub user_id: String,
    pub kind: String,
    pub title: String,
    pub content: String,
    pub data: sqlx::types::Json<Value>,
    pub status: String,
    pub due_at: Option<i64>,
    pub revision: i64,
    pub created_at: i64,
    pub updated_at: i64,
}
#[derive(Deserialize, Default)]
pub struct List {
    pub q: Option<String>,
    pub status: Option<String>,
    pub limit: Option<i64>,
    pub offset: Option<i64>,
}
impl List {
    pub fn bounds(&self) -> Result<(i64, i64)> {
        let limit = self.limit.unwrap_or(50);
        let offset = self.offset.unwrap_or(0);
        if !(1..=200).contains(&limit) || !(0..=1_000_000).contains(&offset) {
            return Err(invalid("分页范围无效"));
        }
        Ok((limit, offset))
    }
}
fn integer(data: &Value, key: &str, min: i64, max: i64) -> Result<()> {
    if let Some(v) = data.get(key) {
        if !v.as_i64().is_some_and(|v| (min..=max).contains(&v)) {
            return Err(invalid(format!("{key} 必须是 {min}–{max} 的整数")));
        }
    }
    Ok(())
}
fn date(data: &Value, key: &str, required: bool) -> Result<()> {
    match data.get(key).and_then(Value::as_str) {
        Some(v) if chrono::NaiveDate::parse_from_str(v, "%Y-%m-%d").is_ok() => Ok(()),
        None if !required && data.get(key).is_none() => Ok(()),
        _ => Err(invalid(format!("{key} 日期格式必须是 YYYY-MM-DD"))),
    }
}
pub fn validate(kind: &str, input: &mut Input) -> Result<()> {
    check_kind(kind)?;
    input.title = input.title.trim().to_owned();
    if input.title.is_empty() || input.title.chars().count() > 255 {
        return Err(invalid("标题须为 1–255 字符"));
    }
    if input.content.len() > 1_000_000 {
        return Err(invalid("内容过长"));
    }
    if !input.data.is_object() || input.data.to_string().len() > 100_000 {
        return Err(invalid("data 必须是大小不超过 100 KB 的对象"));
    }
    if !["open", "done", "cancelled", "draft", "active", "archived"]
        .contains(&input.status.as_str())
    {
        return Err(invalid("状态无效"));
    }
    if let Some(tags) = input.data.get("tags") {
        if !tags.as_array().is_some_and(|a| {
            a.len() <= 30
                && a.iter()
                    .all(|v| v.as_str().is_some_and(|s| s.chars().count() <= 64))
        }) {
            return Err(invalid("tags 须为最多 30 个短字符串"));
        }
    }
    match kind {
        "tasks" => integer(&input.data, "priority", 1, 3)?,
        "events" => {
            let start = input.data["start_at"]
                .as_i64()
                .ok_or_else(|| invalid("start_at 必须为 UTC Unix 秒"))?;
            let end = input.data["end_at"]
                .as_i64()
                .ok_or_else(|| invalid("end_at 必须为 UTC Unix 秒"))?;
            if end <= start {
                return Err(invalid("结束时间须晚于开始时间"));
            }
            input.due_at = Some(start);
            if let Some(repeat) = input.data.get("repeat") {
                if !["none", "daily", "weekly", "monthly"].contains(&repeat.as_str().unwrap_or(""))
                {
                    return Err(invalid("重复规则无效"));
                }
            }
        }
        "expenses" => {
            if input.data.get("amount_cents").is_none() {
                return Err(invalid("amount_cents 必填，金额以分计"));
            }
            integer(&input.data, "amount_cents", 1, 100_000_000_000)?;
            date(&input.data, "date", true)?;
        }
        "fitness" => {
            date(&input.data, "date", true)?;
            integer(&input.data, "minutes", 0, 1440)?;
            integer(&input.data, "calories", 0, 100_000)?;
        }
        "trips" => {
            date(&input.data, "start_date", true)?;
            date(&input.data, "end_date", true)?;
            if input.data["end_date"].as_str() < input.data["start_date"].as_str() {
                return Err(invalid("结束日期须不早于开始日期"));
            }
            integer(&input.data, "budget_cents", 0, 100_000_000_000)?;
        }
        "pages" => {
            if let Some(v) = input.data.get("public") {
                if !v.is_boolean() {
                    return Err(invalid("public 须为布尔值"));
                }
            }
        }
        "skills" => {
            // Skills are reviewed declarative plans, never arbitrary host code.
            if let Some(steps) = input.data.get("steps") {
                if !steps.as_array().is_some_and(|steps| {
                    steps.len() <= 16
                        && steps
                            .iter()
                            .all(|step| step["tool"].is_string() && step["arguments"].is_object())
                }) {
                    return Err(invalid("技能 steps 须为最多 16 个 tool/arguments 对象"));
                }
            }
            if input.data.get("enabled").is_some_and(|v| !v.is_boolean()) {
                return Err(invalid("enabled 须为布尔值"));
            }
        }
        _ => (),
    }
    Ok(())
}
pub async fn get(app: &App, user: &User, kind: &str, record_id: &str) -> Result<Record> {
    check_kind(kind)?;
    sqlx::query_as(
        "SELECT * FROM records WHERE id=? AND user_id=? AND kind=? AND deleted_at IS NULL",
    )
    .bind(record_id)
    .bind(&user.id)
    .bind(kind)
    .fetch_optional(&app.pool)
    .await?
    .ok_or(Error::NotFound)
}
pub async fn list(app: &App, user: &User, kind: &str, filter: &List) -> Result<Value> {
    check_kind(kind)?;
    let (limit, offset) = filter.bounds()?;
    let mut base = QueryBuilder::<Sqlite>::new(" FROM records WHERE user_id=");
    base.push_bind(&user.id)
        .push(" AND kind=")
        .push_bind(kind)
        .push(" AND deleted_at IS NULL");
    if let Some(status) = &filter.status {
        base.push(" AND status=").push_bind(status);
    }
    if let Some(q) = filter.q.as_deref().filter(|s| !s.trim().is_empty()) {
        if q.chars().count() > 200 {
            return Err(invalid("搜索词过长"));
        }
        if q.chars().count() >= 3 {
            base.push(" AND id IN (SELECT id FROM records_fts WHERE records_fts MATCH ")
                .push_bind(format!("\"{}\"", q.replace('"', "\"\"")))
                .push(")");
        } else {
            let escaped = q
                .replace('\\', "\\\\")
                .replace('%', "\\%")
                .replace('_', "\\_");
            base.push(" AND (title LIKE ")
                .push_bind(format!("%{escaped}%"))
                .push(" ESCAPE '\\' OR content LIKE ")
                .push_bind(format!("%{escaped}%"))
                .push(" ESCAPE '\\')");
        }
    }
    // Build twice so SQL bindings remain owned by their corresponding query builder.
    base.push(" ORDER BY updated_at DESC,id LIMIT ")
        .push_bind(limit)
        .push(" OFFSET ")
        .push_bind(offset);
    let sql = format!("SELECT *{}", base.sql());
    let args = base
        .build()
        .take_arguments()
        .map_err(|e| Error::Internal(anyhow::anyhow!(e.to_string())))?
        .unwrap();
    let items: Vec<Record> = sqlx::query_as_with(&sql, args).fetch_all(&app.pool).await?;
    Ok(json!({"items":items,"limit":limit,"offset":offset,"has_more":items.len() as i64==limit}))
}
use sqlx::Execute;
pub async fn create(app: &App, user: &User, kind: &str, mut input: Input) -> Result<Record> {
    validate(kind, &mut input)?;
    let rid = id();
    let time = now();
    let mut tx = app.pool.begin().await?;
    sqlx::query("INSERT INTO records(id,user_id,kind,title,content,data,status,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)")
        .bind(&rid).bind(&user.id).bind(kind).bind(input.title).bind(input.content).bind(sqlx::types::Json(input.data))
        .bind(input.status).bind(input.due_at).bind(time).bind(time).execute(&mut *tx).await?;
    queue_index(&mut tx, user, kind, &rid, 1).await?;
    tx.commit().await?;
    get(app, user, kind, &rid).await
}
pub async fn update(
    app: &App,
    user: &User,
    kind: &str,
    rid: &str,
    mut input: Input,
) -> Result<Record> {
    validate(kind, &mut input)?;
    get(app, user, kind, rid).await?;
    let revision = input
        .revision
        .ok_or_else(|| invalid("更新须携带 revision，避免覆盖其他编辑"))?;
    let mut tx = app.pool.begin().await?;
    let count=sqlx::query("UPDATE records SET title=?,content=?,data=?,status=?,due_at=?,revision=revision+1,updated_at=? WHERE id=? AND user_id=? AND kind=? AND deleted_at IS NULL AND revision=?")
        .bind(input.title).bind(input.content).bind(sqlx::types::Json(input.data)).bind(input.status).bind(input.due_at)
        .bind(now()).bind(rid).bind(&user.id).bind(kind).bind(revision).execute(&mut *tx).await?.rows_affected();
    if count == 0 {
        return Err(Error::Conflict("记录已被修改，请重新加载".into()));
    }
    queue_index(&mut tx, user, kind, rid, revision + 1).await?;
    tx.commit().await?;
    get(app, user, kind, rid).await
}
async fn queue_index(
    tx: &mut sqlx::Transaction<'_, Sqlite>,
    user: &User,
    kind: &str,
    rid: &str,
    revision: i64,
) -> Result<()> {
    if ["notes", "pages", "bookmarks"].contains(&kind) {
        sqlx::query("INSERT INTO jobs(id,user_id,action,params,run_at,dedupe_key,created_at,updated_at) VALUES(?,?,'index',?,?,?,?,?) ON CONFLICT(user_id,dedupe_key) DO NOTHING")
            .bind(id()).bind(&user.id).bind(json!({"id":rid,"kind":kind}).to_string()).bind(now()).bind(format!("index:{rid}:{revision}"))
            .bind(now()).bind(now()).execute(&mut **tx).await?;
    }
    Ok(())
}
pub async fn delete(app: &App, user: &User, kind: &str, rid: &str) -> Result<()> {
    check_kind(kind)?;
    let count=sqlx::query("UPDATE records SET deleted_at=?,revision=revision+1 WHERE id=? AND user_id=? AND kind=? AND deleted_at IS NULL")
        .bind(now()).bind(rid).bind(&user.id).bind(kind).execute(&app.pool).await?.rows_affected();
    if count == 0 {
        return Err(Error::NotFound);
    }
    Ok(())
}
