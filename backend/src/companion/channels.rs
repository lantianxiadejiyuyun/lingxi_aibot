use super::*;
use axum::extract::State;
pub const NAMES: &[&str] = &[
    "inapp",
    "astrbot",
    "feishu",
    "feishu_app",
    "onebot",
    "serverchan",
];
pub async fn validate(app: &App, v: &Value) -> Result<()> {
    if v.is_null() {
        return Ok(());
    }
    let obj = v.as_object().ok_or_else(|| invalid("通知渠道须为对象"))?;
    for (name, c) in obj {
        if !["feishu", "feishu_app", "serverchan"].contains(&name.as_str()) || !c.is_object() {
            return Err(invalid("通知渠道无效"));
        }
        let allowed = match name.as_str() {
            "feishu" => vec!["url", "secret"],
            "feishu_app" => vec!["app_id", "secret", "recipient", "recipient_type"],
            _ => vec!["secret"],
        };
        if c.as_object().unwrap().iter().any(|(k, v)| {
            !allowed.contains(&k.as_str()) || !v.as_str().is_some_and(|s| s.len() <= 4096)
        }) {
            return Err(invalid("通知渠道字段无效"));
        }
        if name == "feishu" {
            if let Some(url) = c["url"].as_str().filter(|s| !s.is_empty()) {
                crate::network::validate_url(app, url).await?;
            }
        }
        if name == "feishu_app"
            && c["recipient_type"]
                .as_str()
                .is_some_and(|s| !["open_id", "user_id", "union_id", "chat_id"].contains(&s))
        {
            return Err(invalid("飞书接收人类型无效"));
        }
        if name == "serverchan" {
            if let Some(key) = c["secret"].as_str().filter(|s| !s.is_empty()) {
                serverchan_url(key)?;
            }
        }
    }
    Ok(())
}
fn present(v: &Value, key: &str) -> bool {
    v[key].as_str().is_some_and(|s| !s.trim().is_empty())
}
pub async fn snapshot(app: &App, user: &User, ch: &str) -> Result<(String, String)> {
    if ch == "inapp" {
        return Ok((user.id.clone(), "inapp".into()));
    }
    if ch == "astrbot" {
        let b = private::binding(app, user).await?;
        if b["bound"] != true {
            return Err(invalid("请先绑定 AstrBot 私聊"));
        }
        return Ok((
            b["session"].as_str().unwrap_or("").into(),
            digest(&b.to_string()),
        ));
    }
    let cfg = crate::integrations::config(app, user).await?;
    if ch == "onebot" {
        let q = crate::qq::config(app, user).await?;
        if !q.private_enabled || q.onebot.url.is_empty() || q.owner_qq.is_empty() {
            return Err(invalid("请先连接个人 QQ"));
        }
        return Ok((q.owner_qq.clone(),digest(&json!({"owner":q.owner_qq,"bot":q.bot_qq,"onebot":q.onebot,"secret":q.inbound.secret}).to_string())));
    }
    let c = &cfg["channels"][ch];
    let ready = match ch {
        "feishu" => present(c, "url"),
        "feishu_app" => present(c, "app_id") && present(c, "secret") && present(c, "recipient"),
        "serverchan" => present(c, "secret"),
        _ => false,
    };
    if !ready {
        return Err(invalid(&format!("请先配置 {ch} 通知渠道")));
    }
    Ok((
        c["recipient"].as_str().unwrap_or(ch).into(),
        digest(&c.to_string()),
    ))
}
pub async fn availability(app: &App, user: &User) -> Result<Value> {
    let mut result = serde_json::Map::new();
    for ch in NAMES {
        result.insert((*ch).into(), json!(snapshot(app, user, ch).await.is_ok()));
    }
    Ok(Value::Object(result))
}
pub async fn settings_api(State(app): State<App>, user: User) -> Result<Json<Value>> {
    let mut v = crate::integrations::config(&app, &user).await?["channels"].clone();
    if v.is_null() {
        v = json!({});
    }
    crate::integrations::redact(&mut v);
    Ok(ok(v))
}
pub async fn save_api(
    State(app): State<App>,
    user: User,
    Json(v): Json<Value>,
) -> Result<Json<Value>> {
    let mut cfg = crate::integrations::config(&app, &user).await?;
    cfg["channels"] = v;
    crate::integrations::save_config(&app, &user, cfg).await?;
    Ok(ok(json!({"saved":true})))
}
pub async fn send_onebot(app: &App, user: &User, text: &str) -> Result<()> {
    let q = crate::qq::config(app, user).await?;
    send_onebot_config(app, &q, text).await
}
async fn send_onebot_config(app: &App, q: &crate::qq::Config, text: &str) -> Result<()> {
    if !q.private_enabled {
        return Err(invalid("个人 QQ 未开启"));
    }
    let url = format!("{}/send_private_msg", q.onebot.url.trim_end_matches('/'));
    let data=post_json(app,&url,&json!({"user_id":q.owner_qq.parse::<u64>().map_err(|_|invalid("主人 QQ 无效"))?,"message":[{"type":"text","data":{"text":text}}],"auto_escape":true}),Some(&q.onebot.token)).await?;
    if data["status"] != "ok" || data["retcode"] != 0 || data["data"]["message_id"].is_null() {
        return Err(invalid("OneBot 未确认发送成功"));
    }
    Ok(())
}
async fn post_json(app: &App, url: &str, body: &Value, token: Option<&str>) -> Result<Value> {
    let mut req = crate::network::client(app, url)
        .await?
        .post(url)
        .json(body)
        .timeout(std::time::Duration::from_secs(20));
    if let Some(t) = token.filter(|s| !s.is_empty()) {
        req = req.bearer_auth(t);
    }
    crate::integrations::json_response(
        req.send()
            .await
            .map_err(|_| Error::Provider("通知发送结果待核实".into()))?,
    )
    .await
}
pub fn serverchan_url(key: &str) -> Result<String> {
    if key.len() > 256 || !key.bytes().all(|b| b.is_ascii_alphanumeric()) {
        return Err(invalid("Server酱 SendKey 格式无效"));
    }
    if key.starts_with("SCT") && key.len() > 6 {
        return Ok(format!("https://sctapi.ftqq.com/{key}.send"));
    }
    if let Some(tail) = key.strip_prefix("sctp") {
        if let Some((uid, suffix)) = tail.split_once('t') {
            if !uid.is_empty() && uid.bytes().all(|b| b.is_ascii_digit()) && !suffix.is_empty() {
                return Ok(format!("https://{uid}.push.ft07.com/send/{key}.send"));
            }
        }
    }
    Err(invalid("请填写 SCT 或 sctp 开头的 SendKey"))
}
pub fn sign_feishu(secret: &str, timestamp: i64) -> String {
    use base64::Engine;
    use hmac::{Hmac, Mac};
    let mac = Hmac::<sha2::Sha256>::new_from_slice(format!("{timestamp}\n{secret}").as_bytes())
        .expect("HMAC accepts any key length");
    base64::engine::general_purpose::STANDARD.encode(mac.finalize().into_bytes())
}
async fn ready_to_send(
    app: &App,
    user: &User,
    ch: &str,
    expected: &str,
    revision: i64,
    fixed: Option<i64>,
) -> Result<bool> {
    if !crate::permissions::allowed(
        app,
        user,
        crate::permissions::Feature::Proactive,
        crate::permissions::Scope::Private,
    )
    .await?
    {
        return Ok(false);
    }
    if !snapshot(app, user, ch)
        .await
        .is_ok_and(|(_, hash)| hash == expected)
    {
        return Ok(false);
    }
    let mut conn = app.pool.acquire().await?;
    let (raw, current): (String, i64) =
        sqlx::query_as("SELECT config,revision FROM companion_state WHERE user_id=?")
            .bind(&user.id)
            .fetch_one(&mut *conn)
            .await?;
    let cfg: Config = serde_json::from_str(&raw).map_err(|_| invalid("陪伴设置无效"))?;
    Ok(current == revision
        && cfg.channels.iter().any(|c| c == ch)
        && guard(
            &mut conn,
            user,
            &cfg,
            fixed.unwrap_or_else(now),
            false,
            false,
        )
        .await?
        .is_none())
}
async fn send(
    app: &App,
    user: &User,
    ch: &str,
    body: &str,
    expected: &str,
    revision: i64,
    fixed: Option<i64>,
) -> Result<bool> {
    let cfg = crate::integrations::config(app, user).await?;
    if !ready_to_send(app, user, ch, expected, revision, fixed).await? {
        return Ok(false);
    }
    if ch == "onebot" {
        let q: crate::qq::Config =
            serde_json::from_value(cfg["qq"].clone()).map_err(|_| invalid("QQ 设置无效"))?;
        let hash = digest(
            &json!({"owner":q.owner_qq,"bot":q.bot_qq,"onebot":q.onebot,"secret":q.inbound.secret})
                .to_string(),
        );
        if hash != expected {
            return Ok(false);
        }
        send_onebot_config(app, &q, body).await?;
        return Ok(true);
    }
    let c = &cfg["channels"][ch];
    if digest(&c.to_string()) != expected {
        return Ok(false);
    }
    let data = match ch {
        "feishu" => {
            let mut payload = json!({"msg_type":"text","content":{"text":body}});
            if let Some(secret) = c["secret"].as_str().filter(|s| !s.is_empty()) {
                let t = now();
                payload["timestamp"] = json!(t.to_string());
                payload["sign"] = json!(sign_feishu(secret, t));
            }
            post_json(app, c["url"].as_str().unwrap_or(""), &payload, None).await?
        }
        "feishu_app" => {
            let origin = "https://open.feishu.cn/open-apis";
            let token = crate::feishu::tenant_token(app, c, origin).await?;
            let kind = c["recipient_type"].as_str().unwrap_or("open_id");
            // Authentication can take time; respect changes made while it was in flight.
            if !ready_to_send(app, user, ch, expected, revision, fixed).await? {
                return Ok(false);
            }
            crate::feishu::send_text(
                app,
                &token,
                origin,
                kind,
                c["recipient"].as_str().unwrap_or(""),
                body,
            )
            .await?;
            return Ok(true);
        }
        "serverchan" => {
            let url = serverchan_url(c["secret"].as_str().unwrap_or(""))?;
            let response = crate::network::client(app, &url)
                .await?
                .post(&url)
                .timeout(std::time::Duration::from_secs(20))
                .form(&[("title", "灵犀有话想和你说"), ("desp", body)])
                .send()
                .await
                .map_err(|_| Error::Provider("推送结果待核实".into()))?;
            crate::integrations::json_response(response).await?
        }
        _ => return Err(invalid("通知渠道无效")),
    };
    if data["code"] != 0 {
        return Err(invalid("通知渠道未确认投递成功"));
    }
    Ok(true)
}
pub async fn record_sent(
    conn: &mut SqliteConnection,
    user: &User,
    aid: &str,
    body: &str,
    at: i64,
) -> Result<()> {
    let mid: Option<i64> =
        sqlx::query_scalar("SELECT message_id FROM companion_activity WHERE id=? AND user_id=?")
            .bind(aid)
            .bind(&user.id)
            .fetch_one(&mut *conn)
            .await?;
    if mid.is_some() {
        return Ok(());
    }
    let existing: Option<String> =
        sqlx::query_scalar("SELECT conversation_id FROM companion_state WHERE user_id=?")
            .bind(&user.id)
            .fetch_one(&mut *conn)
            .await?;
    let cid = if let Some(cid) = existing {
        cid
    } else {
        let cid = id();
        sqlx::query("INSERT INTO conversations(id,user_id,title,created_at,updated_at) VALUES(?,?,'灵犀日常',?,?)").bind(&cid).bind(&user.id).bind(at).bind(at).execute(&mut *conn).await?;
        cid
    };
    let mid = sqlx::query(
        "INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,'assistant',?,?)",
    )
    .bind(&cid)
    .bind(body)
    .bind(at)
    .execute(&mut *conn)
    .await?
    .last_insert_rowid();
    sqlx::query("UPDATE conversations SET updated_at=? WHERE id=? AND user_id=?")
        .bind(at)
        .bind(&cid)
        .bind(&user.id)
        .execute(&mut *conn)
        .await?;
    sqlx::query("UPDATE companion_state SET last_sent_at=?,conversation_id=? WHERE user_id=?")
        .bind(at)
        .bind(&cid)
        .bind(&user.id)
        .execute(&mut *conn)
        .await?;
    sqlx::query("UPDATE companion_activity SET message_id=? WHERE id=? AND user_id=?")
        .bind(mid)
        .bind(aid)
        .bind(&user.id)
        .execute(&mut *conn)
        .await?;
    Ok(())
}
pub async fn delivery_ready(
    conn: &mut SqliteConnection,
    user: &User,
    cfg: &Config,
    aid: &str,
    at: i64,
) -> Result<bool> {
    // Count actual delivery attempts, so an offline bridge cannot flush its backlog in a burst.
    // Other channels for this same activity are still allowed.
    let last: Option<i64> = sqlx::query_scalar("SELECT MAX(updated_at) FROM companion_delivery WHERE user_id=? AND activity_id<>? AND status IN ('sent','sending','unknown')")
        .bind(&user.id).bind(aid).fetch_one(&mut *conn).await?;
    Ok(!last.is_some_and(|t| at < t + cfg.min_interval_minutes * 60))
}
pub async fn dispatch(
    app: &App,
    user: &User,
    aid: &str,
    body: &str,
    fixed: Option<i64>,
    cancel: &CancellationToken,
) -> Result<()> {
    let clock = || fixed.unwrap_or_else(now);
    let rows:Vec<(i64,String,String,String,i64)>=sqlx::query_as("SELECT id,channel,target,config_digest,revision FROM companion_delivery WHERE user_id=? AND activity_id=? AND channel<>'astrbot' AND status='pending' ORDER BY id").bind(&user.id).bind(aid).fetch_all(&app.pool).await?;
    for (did, ch, target, hash, revision) in rows {
        let permission = crate::permissions::allowed(
            app,
            user,
            crate::permissions::Feature::Proactive,
            crate::permissions::Scope::Private,
        )
        .await?;
        let current = snapshot(app, user, &ch).await;
        let mut tx = app.pool.begin().await?;
        sqlx::query("UPDATE companion_state SET revision=revision WHERE user_id=?")
            .bind(&user.id)
            .execute(&mut *tx)
            .await?;
        let (raw, rev): (String, i64) =
            sqlx::query_as("SELECT config,revision FROM companion_state WHERE user_id=?")
                .bind(&user.id)
                .fetch_one(&mut *tx)
                .await?;
        let cfg: Config = serde_json::from_str(&raw).map_err(|_| invalid("陪伴设置无效"))?;
        let block = guard(&mut tx, user, &cfg, clock(), false, false).await?;
        if !permission
            || cancel.is_cancelled()
            || rev != revision
            || !cfg.channels.contains(&ch)
            || current.ok() != Some((target, hash.clone()))
            || block.is_some()
            || !delivery_ready(&mut tx, user, &cfg, aid, clock()).await?
        {
            sqlx::query("UPDATE companion_delivery SET status='cancelled',error='发送前检查未通过',updated_at=? WHERE id=? AND status='pending'").bind(clock()).bind(did).execute(&mut *tx).await?;
            tx.commit().await?;
            continue;
        }
        let changed=sqlx::query("UPDATE companion_delivery SET status='sending',lease_until=?,updated_at=? WHERE id=? AND status='pending'").bind(clock()+300).bind(clock()).bind(did).execute(&mut *tx).await?.rows_affected();
        if changed == 0 {
            continue;
        }
        if ch == "inapp" {
            sqlx::query("INSERT INTO notifications(id,user_id,title,body,dedupe_key,created_at) VALUES(?,?,'灵犀有话想和你说',?,?,?) ON CONFLICT DO NOTHING").bind(id()).bind(&user.id).bind(body).bind(format!("companion:{aid}")).bind(clock()).execute(&mut *tx).await?;
            record_sent(&mut tx, user, aid, body, clock()).await?;
            sqlx::query("UPDATE companion_delivery SET status='sent',updated_at=? WHERE id=?")
                .bind(clock())
                .bind(did)
                .execute(&mut *tx)
                .await?;
            tx.commit().await?;
            continue;
        }
        tx.commit().await?;
        let result = send(app, user, &ch, body, &hash, revision, fixed).await;
        let (status, error) = match &result {
            Ok(true) => ("sent", ""),
            Ok(false) => ("cancelled", "发送前设置或时段已改变"),
            Err(_) => ("unknown", "未获成功确认，请核实渠道；不会自动重发"),
        };
        let mut tx = app.pool.begin().await?;
        sqlx::query("UPDATE companion_delivery SET status=?,error=?,updated_at=? WHERE id=? AND user_id=? AND status='sending'").bind(status).bind(error).bind(clock()).bind(did).bind(&user.id).execute(&mut *tx).await?;
        if matches!(result, Ok(true)) {
            record_sent(&mut tx, user, aid, body, clock()).await?;
        }
        tx.commit().await?;
    }
    let states: Vec<String> = sqlx::query_scalar(
        "SELECT status FROM companion_delivery WHERE activity_id=? AND user_id=?",
    )
    .bind(aid)
    .bind(&user.id)
    .fetch_all(&app.pool)
    .await?;
    sqlx::query("UPDATE companion_activity SET status=?,updated_at=? WHERE id=? AND user_id=?")
        .bind(aggregate(
            &states.iter().map(String::as_str).collect::<Vec<_>>(),
        ))
        .bind(clock())
        .bind(aid)
        .bind(&user.id)
        .execute(&app.pool)
        .await?;
    Ok(())
}
