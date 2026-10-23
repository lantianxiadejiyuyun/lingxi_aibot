//! Bounded, durable message bursts. Uncertain sends are never replayed.
use super::{Config, config, store};
use crate::{
    auth::User,
    db::now,
    error::{Error, Result, invalid},
    state::App,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::SqlitePool;
use tokio_util::sync::CancellationToken;

#[derive(Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Style {
    pub enabled: bool,
    pub max_messages: usize,
    pub interval_secs: u64,
}
impl Default for Style {
    fn default() -> Self {
        Self {
            enabled: false,
            max_messages: 3,
            interval_secs: 2,
        }
    }
}
impl Style {
    pub fn validate(&self) -> Result<()> {
        if !(2..=4).contains(&self.max_messages) || !(1..=10).contains(&self.interval_secs) {
            return Err(invalid("分条回复最多2–4条，间隔1–10秒"));
        }
        Ok(())
    }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Followup {
    pub enabled: bool,
    pub delay_minutes: i64,
    pub max_followups: i64,
    pub daily_limit: i64,
}
impl Default for Followup {
    fn default() -> Self {
        Self {
            enabled: false,
            delay_minutes: 10,
            max_followups: 3,
            daily_limit: 6,
        }
    }
}
impl Followup {
    pub fn validate(&self) -> Result<()> {
        if !(1..=60).contains(&self.delay_minutes)
            || !(1..=5).contains(&self.max_followups)
            || !(1..=12).contains(&self.daily_limit)
        {
            return Err(invalid(
                "延迟补充等待1–60分钟，每轮上限1–5次、24小时上限1–12次",
            ));
        }
        Ok(())
    }
}

pub fn style(cfg: &Config, group: Option<&str>) -> Style {
    group
        .and_then(|g| cfg.group_settings.get(g))
        .map(|g| g.reply_style.clone())
        .unwrap_or_else(|| {
            if group.is_none() {
                cfg.reply_style.clone()
            } else {
                Style::default()
            }
        })
}
pub fn followup(cfg: &Config, group: Option<&str>) -> Followup {
    group
        .and_then(|g| cfg.group_settings.get(g))
        .map(|g| g.followup.clone())
        .unwrap_or_else(|| {
            if group.is_none() {
                cfg.followup.clone()
            } else {
                Followup::default()
            }
        })
}
pub fn key(cfg: &Config, group: Option<&str>) -> String {
    if let Some(g) = group {
        super::sessions::reply_key(cfg, g)
    } else {
        crate::db::digest(&json!({"owner":cfg.owner_qq,"bot":cfg.bot_qq,"enabled":cfg.private_enabled,"onebot":cfg.onebot,"profile":cfg.profile_id,"style":cfg.reply_style,"followup":cfg.followup}).to_string())
    }
}
pub async fn unchanged(app: &App, user: &User, cfg: &Config, group: Option<&str>) -> Result<bool> {
    let fresh = config(app, user).await?;
    let active = if let Some(g) = group {
        fresh.for_group(g).filter(|c| c.enabled)
    } else {
        fresh.private_enabled.then_some(fresh)
    };
    Ok(active.is_some_and(|c| key(&c, group) == key(cfg, group)))
}

pub fn split(text: &str, style: &Style) -> Vec<String> {
    let text = text.trim();
    if !style.enabled || text.contains('`') || text.contains("http://") || text.contains("https://")
    {
        return vec![text.into()];
    }
    let mut pieces: Vec<String> = text
        .split("\n\n")
        .map(str::trim)
        .filter(|p| !p.is_empty())
        .map(String::from)
        .collect();
    if pieces.len() == 1 && text.chars().count() > 60 {
        pieces.clear();
        let mut part = String::new();
        for c in text.chars() {
            part.push(c);
            if "。！？!?".contains(c) && part.chars().count() >= 35 {
                pieces.push(std::mem::take(&mut part));
            }
        }
        if !part.is_empty() {
            pieces.push(part);
        }
    }
    if pieces.len() > style.max_messages {
        let rest = pieces.split_off(style.max_messages - 1);
        pieces.push(rest.join("\n\n"));
    }
    pieces
}
pub fn prompt(style: &Style) -> &'static str {
    if style.enabled {
        "用自然口语回复，可按完整意思用空行分成两三段，每段可以单独发送；简单问题一句即可，不凑条数，不输出分条标记或动作旁白。"
    } else {
        ""
    }
}

pub async fn storage<'a>(app: &'a App, main: bool) -> Result<(&'a SqlitePool, &'static str)> {
    Ok(if main {
        (&app.pool, "qq_reply_parts")
    } else {
        (store::pool(app).await?, "reply_parts")
    })
}
pub async fn parts(app: &App, user: &User, key: &str, main: bool) -> Result<Vec<Value>> {
    let (pool, table) = storage(app, main).await?;
    let rows:Vec<(i64,String,String)>=sqlx::query_as(&format!("SELECT position,body,status FROM {table} WHERE user_id=? AND delivery_key=? ORDER BY position")).bind(&user.id).bind(key).fetch_all(pool).await?;
    Ok(rows
        .into_iter()
        .map(|(position, body, status)| json!({"position":position,"body":body,"status":status}))
        .collect())
}

#[allow(clippy::too_many_arguments)]
pub async fn send(
    app: &App,
    user: &User,
    cfg: &Config,
    group: Option<&str>,
    mention: Option<&str>,
    delivery_key: &str,
    text: &str,
    main: bool,
    cancel: &CancellationToken,
    anchor: Option<&str>,
) -> Result<usize> {
    let chunks = split(text, &style(cfg, group));
    if chunks.is_empty() || chunks.iter().any(|s| s.trim().is_empty()) {
        return Err(invalid("回复不能为空"));
    }
    let (pool, table) = storage(app, main).await?;
    let mut tx = pool.begin().await?;
    for (i, body) in chunks.iter().enumerate() {
        sqlx::query(&format!("INSERT INTO {table}(user_id,delivery_key,position,body,status,created_at,updated_at) VALUES(?,?,?,?,'pending',?,?) ON CONFLICT DO NOTHING")).bind(&user.id).bind(delivery_key).bind(i as i64).bind(body).bind(now()).bind(now()).execute(&mut *tx).await?;
    }
    tx.commit().await?;
    let result=async {
        for (i,body) in chunks.iter().enumerate(){
            if i>0 {tokio::select!{_=cancel.cancelled()=>return Err(Error::Cancelled),_=app.shutdown.cancelled()=>return Err(Error::Cancelled),_=tokio::time::sleep(std::time::Duration::from_secs(style(cfg,group).interval_secs))=>{}}}
            if cancel.is_cancelled()||app.shutdown.is_cancelled() {return Err(Error::Cancelled);}
            if !unchanged(app,user,cfg,group).await? {return Err(invalid("会话设置已修改，停止剩余消息"));}
            if let Some(anchor)=anchor {if super::outreach::anchor(app,user,group).await? != anchor {return Err(invalid("收到新消息，取消补充"));}}
            let claimed=sqlx::query(&format!("UPDATE {table} SET status='sending',updated_at=? WHERE user_id=? AND delivery_key=? AND position=? AND status='pending' AND body=?")).bind(now()).bind(&user.id).bind(delivery_key).bind(i as i64).bind(body).execute(pool).await?.rows_affected();
            if claimed!=1{return Err(invalid("这条消息已尝试发送，不自动重发"));}
            let mut segments=vec![];
            if i==0 {if let Some(qq)=mention {segments.push(json!({"type":"at","data":{"qq":qq}}));}}
            segments.push(json!({"type":"text","data":{"text":body}}));
            let (action,payload)=if let Some(group)=group {("send_group_msg",json!({"group_id":group,"message":segments,"auto_escape":true}))}else{("send_private_msg",json!({"user_id":cfg.owner_qq.parse::<u64>().map_err(|_|invalid("主人QQ无效"))?,"message":segments,"auto_escape":true}))};
            let url=format!("{}/{action}",cfg.onebot.url.trim_end_matches('/'));
            let mut req=crate::network::client(app,&url).await?.post(&url).json(&payload).timeout(std::time::Duration::from_secs(20));
            if !cfg.onebot.token.is_empty(){req=req.bearer_auth(&cfg.onebot.token);}
            let response=req.send().await.map_err(|_|Error::Provider("发送结果不确定，未自动重发".into()))?;
            let value=crate::integrations::json_response(response).await?;
            if value["status"]!="ok"||value["retcode"]!=0||value["data"]["message_id"].is_null(){return Err(Error::Provider("OneBot 未确认发送成功".into()));}
            sqlx::query(&format!("UPDATE {table} SET status='sent',updated_at=? WHERE user_id=? AND delivery_key=? AND position=?")).bind(now()).bind(&user.id).bind(delivery_key).bind(i as i64).execute(pool).await?;
        }
        Ok(chunks.len())
    }.await;
    if result.is_err() {
        sqlx::query(&format!("UPDATE {table} SET status=CASE WHEN status='sending' THEN 'unknown' ELSE 'cancelled' END,updated_at=? WHERE user_id=? AND delivery_key=? AND status IN ('pending','sending')")).bind(now()).bind(&user.id).bind(delivery_key).execute(pool).await?;
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn split_preserves_all_content_and_protects_code() {
        let s = Style {
            enabled: true,
            max_messages: 2,
            ..Default::default()
        };
        assert_eq!(
            split("第一句\n\n第二句\n\n第三句", &s),
            vec!["第一句", "第二句\n\n第三句"]
        );
        let code = "运行 `print(1)`\n\n然后看结果";
        assert_eq!(split(code, &s), vec![code]);
        assert_eq!(
            split("第一句\n\n第二句", &Style::default()),
            vec!["第一句\n\n第二句"]
        );
    }
}
