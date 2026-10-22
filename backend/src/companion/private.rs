use super::*;
use axum::extract::Path;
pub fn valid_session(s: &str) -> bool {
    let p: Vec<_> = s.splitn(3, ':').collect();
    !s.is_empty()
        && s.len() <= 512
        && s == s.trim()
        && !s.chars().any(char::is_control)
        && p.len() == 3
        && !p[0].is_empty()
        && p[1] == "FriendMessage"
        && !p[2].is_empty()
}
pub async fn binding(app: &App, user: &User) -> Result<Value> {
    let b:Option<(String,Option<String>,i64)>=sqlx::query_as("SELECT external_id,conversation_id,updated_at FROM private_bindings WHERE user_id=? AND channel='astrbot'").bind(&user.id).fetch_optional(&app.pool).await?;
    Ok(match b {
        Some((session, cid, at)) => {
            json!({"bound":true,"enabled":true,"session":session,"conversation_id":cid,"updated_at":at})
        }
        None => json!({"bound":false,"enabled":false,"session":"","conversation_id":null}),
    })
}
pub async fn binding_api(State(app): State<App>, user: User) -> Result<Json<Value>> {
    Ok(ok(binding(&app, &user).await?))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Binding {
    pub session: String,
}
async fn bind_conversation(
    app: &App,
    user: &User,
    channel: &str,
    external: &str,
) -> Result<String> {
    ensure(app, user).await?;
    let mut tx = app.pool.begin().await?;
    sqlx::query("UPDATE companion_state SET revision=revision WHERE user_id=?")
        .bind(&user.id)
        .execute(&mut *tx)
        .await?;
    let old: Option<(String, Option<String>)> = sqlx::query_as(
        "SELECT external_id,conversation_id FROM private_bindings WHERE user_id=? AND channel=?",
    )
    .bind(&user.id)
    .bind(channel)
    .fetch_optional(&mut *tx)
    .await?;
    if let Some((old, Some(cid))) = &old {
        if old == external {
            return Ok(cid.clone());
        }
    }
    let cid = id();
    sqlx::query(
        "INSERT INTO conversations(id,user_id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
    )
    .bind(&cid)
    .bind(&user.id)
    .bind(if channel == "astrbot" {
        "AstrBot 私聊"
    } else {
        "QQ 主人私聊"
    })
    .bind(now())
    .bind(now())
    .execute(&mut *tx)
    .await?;
    let r=sqlx::query("INSERT INTO private_bindings(user_id,channel,external_id,conversation_id,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id,channel) DO UPDATE SET external_id=excluded.external_id,conversation_id=excluded.conversation_id,updated_at=excluded.updated_at").bind(&user.id).bind(channel).bind(external).bind(&cid).bind(now()).execute(&mut *tx).await;
    if r.as_ref().is_err_and(|e| {
        e.as_database_error()
            .is_some_and(|e| e.is_unique_violation())
    }) {
        return Err(Error::Conflict("这个私聊已绑定其他账户".into()));
    }
    r?;
    sqlx::query("UPDATE companion_delivery SET status='cancelled',error='私聊绑定已变更',updated_at=? WHERE user_id=? AND channel=? AND status='pending'").bind(now()).bind(&user.id).bind(channel).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(cid)
}
pub async fn bind_api(
    State(app): State<App>,
    user: User,
    Json(v): Json<Binding>,
) -> Result<Json<Value>> {
    if !valid_session(&v.session) {
        return Err(invalid(
            "仅支持 platform_id:FriendMessage:session_id 私聊身份",
        ));
    }
    bind_conversation(&app, &user, "astrbot", &v.session).await?;
    Ok(ok(binding(&app, &user).await?))
}
pub async fn unbind_api(
    State(app): State<App>,
    user: User,
    Json(v): Json<Binding>,
) -> Result<Json<Value>> {
    if !valid_session(&v.session) {
        return Err(invalid("私聊身份无效"));
    }
    ensure(&app, &user).await?;
    let mut tx = app.pool.begin().await?;
    sqlx::query("UPDATE companion_state SET revision=revision WHERE user_id=?")
        .bind(&user.id)
        .execute(&mut *tx)
        .await?;
    let old: Option<String> = sqlx::query_scalar(
        "SELECT external_id FROM private_bindings WHERE user_id=? AND channel='astrbot'",
    )
    .bind(&user.id)
    .fetch_optional(&mut *tx)
    .await?;
    if old.as_ref().is_some_and(|s| s != &v.session) {
        return Err(Error::Conflict("私聊绑定已变更".into()));
    }
    sqlx::query("DELETE FROM private_bindings WHERE user_id=? AND channel='astrbot'")
        .bind(&user.id)
        .execute(&mut *tx)
        .await?;
    sqlx::query("UPDATE companion_delivery SET status='cancelled',error='AstrBot 已解绑',updated_at=? WHERE user_id=? AND channel='astrbot' AND status='pending'").bind(now()).bind(&user.id).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(ok(binding(&app, &user).await?))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Chat {
    pub conversation_id: String,
    pub message: String,
    pub request_id: String,
    #[serde(default)]
    pub stream: bool,
}
pub async fn chat_api(
    State(app): State<App>,
    user: User,
    Json(v): Json<Chat>,
) -> Result<Json<Value>> {
    let b = binding(&app, &user).await?;
    if b["bound"] != true || b["conversation_id"].as_str() != Some(&v.conversation_id) || v.stream {
        return Err(Error::Forbidden);
    }
    let session = crate::chat::prepare(
        &app,
        &user,
        &v.conversation_id,
        crate::chat::Send {
            message: v.message,
            request_id: v.request_id,
        },
    )
    .await?;
    let reply = crate::chat::run(app.clone(), session, None, app.shutdown.child_token()).await?;
    Ok(ok(
        json!({"reply":reply,"conversation_id":v.conversation_id}),
    ))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Claim {
    pub limit: i64,
}
pub async fn claim_api(
    State(app): State<App>,
    user: User,
    Json(v): Json<Claim>,
) -> Result<Json<Value>> {
    if !(1..=10).contains(&v.limit) {
        return Err(invalid("limit 为1–10"));
    }
    let b = binding(&app, &user).await?;
    if b["bound"] != true {
        return Ok(ok(json!([])));
    }
    let hash = digest(&b.to_string());
    let mut tx = app.pool.begin().await?;
    sqlx::query("UPDATE companion_state SET revision=revision WHERE user_id=?")
        .bind(&user.id)
        .execute(&mut *tx)
        .await?;
    let (raw, revision): (String, i64) =
        sqlx::query_as("SELECT config,revision FROM companion_state WHERE user_id=?")
            .bind(&user.id)
            .fetch_one(&mut *tx)
            .await?;
    let cfg: Config = serde_json::from_str(&raw).map_err(|_| invalid("陪伴配置损坏"))?;
    if guard(&mut tx, &user, &cfg, now(), false, false)
        .await?
        .is_some()
    {
        return Ok(ok(json!([])));
    }
    let rows:Vec<(i64,String,String)>=sqlx::query_as("SELECT d.id,d.activity_id,a.body FROM companion_delivery d JOIN companion_activity a ON a.id=d.activity_id WHERE d.user_id=? AND d.channel='astrbot' AND d.status='pending' AND d.target=? AND d.config_digest=? AND d.revision=? AND d.expires_at>? ORDER BY d.id LIMIT ?").bind(&user.id).bind(b["session"].as_str().unwrap_or("")).bind(hash).bind(revision).bind(now()).bind(v.limit).fetch_all(&mut *tx).await?;
    let mut out = vec![];
    for (did, aid, text) in rows {
        if !channels::delivery_ready(&mut tx, &user, &cfg, &aid, now()).await? {
            break;
        }
        let lease = id();
        sqlx::query("UPDATE companion_delivery SET status='sending',lease_token=?,lease_until=?,updated_at=? WHERE id=?").bind(&lease).bind(now()+300).bind(now()).bind(did).execute(&mut *tx).await?;
        out.push(json!({"id":did,"session":b["session"],"text":text,"lease_token":lease,"conversation_id":b["conversation_id"]}));
    }
    tx.commit().await?;
    Ok(ok(out))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Ack {
    pub lease_token: String,
    pub outcome: String,
}
pub async fn ack_api(
    State(app): State<App>,
    user: User,
    Path(did): Path<i64>,
    Json(v): Json<Ack>,
) -> Result<Json<Value>> {
    if !["sent", "failed"].contains(&v.outcome.as_str()) {
        return Err(invalid("outcome 为 sent/failed"));
    }
    let mut tx = app.pool.begin().await?;
    sqlx::query("UPDATE companion_state SET revision=revision WHERE user_id=?")
        .bind(&user.id)
        .execute(&mut *tx)
        .await?;
    let row:Option<(String,String,Option<String>,String)>=sqlx::query_as("SELECT d.activity_id,d.status,d.lease_token,a.body FROM companion_delivery d JOIN companion_activity a ON a.id=d.activity_id WHERE d.id=? AND d.user_id=? AND d.channel='astrbot'").bind(did).bind(&user.id).fetch_optional(&mut *tx).await?;
    let (aid, status, token, body) = row.ok_or(Error::NotFound)?;
    if token.as_deref() != Some(v.lease_token.as_str())
        || !["sent", "failed", "sending", "unknown"].contains(&status.as_str())
    {
        return Err(Error::Conflict("投递租约无效".into()));
    }
    if status == "sent" || status == "failed" {
        if v.outcome == status {
            return Ok(ok(json!({"id":did,"status":status})));
        }
        return Err(Error::Conflict("消息已经确认处理结果".into()));
    }
    sqlx::query("UPDATE companion_delivery SET status=?,error=?,updated_at=? WHERE id=?")
        .bind(&v.outcome)
        .bind(if v.outcome == "sent" {
            ""
        } else {
            "AstrBot 投递失败，未自动重发"
        })
        .bind(now())
        .bind(did)
        .execute(&mut *tx)
        .await?;
    if v.outcome == "sent" {
        channels::record_sent(&mut tx, &user, &aid, &body, now()).await?;
    }
    tx.commit().await?;
    Ok(ok(json!({"id":did,"status":v.outcome})))
}
pub async fn receive_qq(
    app: &App,
    user: &User,
    value: &Value,
    cfg: &crate::qq::Config,
) -> Result<Value> {
    if !cfg.private_enabled
        || crate::qq::numeric_id(&value["user_id"]).as_deref() != Some(&cfg.owner_qq)
        || crate::qq::numeric_id(&value["self_id"]).as_deref() != Some(&cfg.bot_qq)
    {
        return Ok(json!({"ignored":true}));
    }
    let (text, _) = crate::qq::api::text_message(&value["message"], &cfg.bot_qq);
    let text = text.trim();
    if text.is_empty() {
        return Ok(json!({"ignored":true}));
    }
    if text.chars().count() > 4000 {
        return Err(invalid("私聊消息最多4000字"));
    }
    let mid = value["message_id"]
        .as_i64()
        .map(|n| n.to_string())
        .or_else(|| {
            value["message_id"]
                .as_str()
                .filter(|s| !s.is_empty() && s.len() <= 64)
                .map(String::from)
        })
        .ok_or_else(|| invalid("message_id 必填"))?;
    let key = digest(&format!("private:{}:{}:{mid}", user.id, cfg.bot_qq));
    sqlx::query("INSERT INTO qq_private_events(id,user_id,content,bot_qq,owner_qq,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING").bind(&key).bind(&user.id).bind(text).bind(&cfg.bot_qq).bind(&cfg.owner_qq).bind(now()).bind(now()).execute(&app.pool).await?;
    crate::jobs::enqueue(
        app,
        user,
        crate::jobs::Input {
            action: "qq_private".into(),
            params: json!({"id":key}),
            run_at: None,
            request_id: Some(format!("private:{key}")),
        },
    )
    .await?;
    Ok(json!({"accepted":true,"storage":"main"}))
}
pub async fn process_qq(
    app: &App,
    user: &User,
    params: &Value,
    cancel: &CancellationToken,
) -> Result<Value> {
    let key = params["id"].as_str().unwrap_or("");
    let q = crate::qq::config(app, user).await?;
    let row:Option<(String,String,String)>=sqlx::query_as("UPDATE qq_private_events SET status='processing',updated_at=? WHERE id=? AND user_id=? AND status='pending' RETURNING content,bot_qq,owner_qq").bind(now()).bind(key).bind(&user.id).fetch_optional(&app.pool).await?;
    let Some((text, bot, owner)) = row else {
        return Ok(json!({"duplicate":true}));
    };
    let result=async {
  if !q.private_enabled||q.owner_qq!=owner||q.bot_qq!=bot{return Err(Error::Forbidden);}
  let cid=bind_conversation(app,user,"onebot",&owner).await?;
  sqlx::query("UPDATE conversations SET profile_id=? WHERE id=? AND user_id=?").bind(&q.profile_id).bind(&cid).bind(&user.id).execute(&app.pool).await?;
  let session=crate::chat::prepare(app,user,&cid,crate::chat::Send{message:text,request_id:format!("qq-private:{key}")}).await?;
  let reply=crate::chat::run(app.clone(),session,None,cancel.clone()).await?;
  let current=crate::qq::config(app,user).await?;if !current.private_enabled||current.owner_qq!=owner||current.bot_qq!=bot{return Err(Error::Forbidden);}
  sqlx::query("UPDATE qq_private_events SET status='sending',reply=?,updated_at=? WHERE id=? AND user_id=?").bind(&reply).bind(now()).bind(key).bind(&user.id).execute(&app.pool).await?;
  channels::send_onebot(app,user,&reply).await?;
  sqlx::query("UPDATE qq_private_events SET status='sent',updated_at=? WHERE id=? AND user_id=?").bind(now()).bind(key).bind(&user.id).execute(&app.pool).await?;Ok(json!({"sent":true,"conversation_id":cid}))
 }.await;
    if result.is_err() {
        sqlx::query("UPDATE qq_private_events SET status=CASE WHEN status='sending' THEN 'unknown' ELSE 'failed' END,updated_at=? WHERE id=? AND user_id=? AND status<>'sent'").bind(now()).bind(key).bind(&user.id).execute(&app.pool).await?;
    }
    result
}
pub async fn reconcile(app: &App) -> Result<()> {
    let rows:Vec<(String,String)>=sqlx::query_as("SELECT id,user_id FROM qq_private_events WHERE status='pending' ORDER BY created_at LIMIT 100").fetch_all(&app.pool).await?;
    for (key, uid) in rows {
        if let Ok(user) = crate::auth::user(app, &uid).await {
            crate::jobs::enqueue(
                app,
                &user,
                crate::jobs::Input {
                    action: "qq_private".into(),
                    params: json!({"id":key}),
                    run_at: None,
                    request_id: Some(format!("private:{key}")),
                },
            )
            .await?;
        }
    }
    sqlx::query("UPDATE qq_private_events SET status=CASE WHEN status='sending' THEN 'unknown' ELSE 'failed' END,updated_at=? WHERE status IN ('processing','sending') AND updated_at<?").bind(now()).bind(now()-600).execute(&app.pool).await?;
    Ok(())
}
