//! One reversible preset for conversational tone and existing delivery limits.
use crate::{
    auth::User,
    db::digest,
    error::{Error, Result, invalid},
    settings,
    state::App,
};
use axum::{Json, extract::State};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;

#[derive(Clone, Copy, Default, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    Quiet,
    #[default]
    Natural,
    Lively,
}
impl Mode {
    fn limits(self) -> (usize, i64, i64, i64, i64, i64) {
        // Paragraphs, follow-up delay/round/day, group reply cooldown/hour.
        match self {
            Self::Quiet => (2, 20, 1, 2, 600, 2),
            Self::Natural => (2, 15, 1, 3, 180, 5),
            Self::Lively => (3, 10, 2, 4, 90, 8),
        }
    }
    fn tone(self) -> &'static str {
        match self {
            Self::Quiet => "安静陪伴：日常尽量一两句，给对方留空间；被认真提问时仍完整回答。",
            Self::Natural => {
                "自然相处：像熟悉的朋友，有自己的观点，能接梗也能认真聊；一两个意思就够了，不刻意卖萌。"
            }
            Self::Lively => {
                "轻松活跃：可以更有兴致地接梗、打趣和分享具体想法；热情不等于连续追问或抢话。"
            }
        }
    }
}
#[derive(Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
struct Saved {
    mode: Mode,
    fingerprint: String,
}

pub const NATURAL_STYLE: &str = "交流要有来有回：先接住对方这句话的重点，再决定是否补充。日常短话短回，认真提问再展开；不复述提问，不逐句点评，不固定用客服式开场、总结或反问收尾。对方只说好的、谢谢、晚安时简短收住；没有新内容就不续杯，不因对方沉默催回复。可以有主见、温和打趣；对方认真或不舒服时收住，不强行安慰、训话或给建议。沿用已有称呼和话题，但不每条叫名字，不刻意展示记忆。只输出说出口的话，不写括号里的动作、神态或内心旁白；正常括号说明和代码不受影响。不编造线下经历、感官体验或共同回忆，不假装真人。任务、代码和详细说明按实际需要写完整，不能为了口语化丢信息。";
pub async fn prompt(app: &App, user: &User) -> Result<String> {
    let saved: Saved = serde_json::from_value(
        settings::get(app, user, "interaction")
            .await?
            .unwrap_or(json!({})),
    )
    .map_err(|_| invalid("相处设置损坏"))?;
    Ok(format!("{NATURAL_STYLE}\n{}", saved.mode.tone()))
}

type Snapshot = BTreeMap<String, String>;
fn decode(app: &App, state: &Snapshot) -> Result<(Value, Saved)> {
    let integrations = if let Some(raw) = state.get("integrations") {
        let encrypted: String = serde_json::from_str(raw).map_err(|_| invalid("接入配置损坏"))?;
        serde_json::from_str(&app.secrets.decrypt(&encrypted)?)
            .map_err(|_| invalid("接入配置损坏"))?
    } else {
        json!({})
    };
    let saved = serde_json::from_str(state.get("interaction").map(String::as_str).unwrap_or("{}"))
        .map_err(|_| invalid("相处设置损坏"))?;
    Ok((integrations, saved))
}
fn revision(state: &Snapshot) -> String {
    digest(&json!(state).to_string())
}
fn controlled(cfg: &crate::qq::Config) -> Value {
    let followup = |p: &crate::qq::dialogue::Followup| {
        json!([p.delay_minutes, p.max_followups, p.daily_limit])
    };
    let groups:BTreeMap<_,_>=cfg.groups.iter().map(|id|{
        let g=cfg.group_settings.get(id).cloned().unwrap_or_default();
        (id,json!({"style":g.reply_style,"followup":followup(&g.followup),"limits":[json!(g.decision.limits_enabled),json!(g.decision.cooldown_secs),json!(g.decision.max_replies_per_hour)]}))
    }).collect();
    json!({"style":cfg.reply_style,"followup":followup(&cfg.followup),"groups":groups})
}
fn view_data(app: &App, state: &Snapshot) -> Result<Value> {
    let (integrations, saved) = decode(app, state)?;
    let cfg: crate::qq::Config =
        serde_json::from_value(integrations.get("qq").cloned().unwrap_or(json!({})))
            .map_err(|_| invalid("QQ 配置损坏"))?;
    let choices:Vec<_>=[Mode::Quiet,Mode::Natural,Mode::Lively].into_iter().map(|mode|{
        let (paragraphs,delay,round,day,cooldown,hour)=mode.limits();
        json!({"mode":mode,"paragraphs":paragraphs,"followup_delay":delay,"followup_round":round,"followup_daily":day,"group_cooldown":cooldown,"group_hourly":hour})
    }).collect();
    Ok(
        json!({"mode":saved.mode,"configured":state.contains_key("interaction"),"customized":!saved.fingerprint.is_empty()&&saved.fingerprint!=digest(&controlled(&cfg).to_string()),"revision":revision(state),"group_count":cfg.groups.len(),"presets":choices}),
    )
}
pub async fn view(State(app): State<App>, user: User) -> Result<Json<Value>> {
    let rows: Vec<(String, String)> = sqlx::query_as(
        "SELECT key,value FROM settings WHERE user_id=? AND key IN ('integrations','interaction')",
    )
    .bind(&user.id)
    .fetch_all(&app.pool)
    .await?;
    Ok(Json(
        json!({"ok":true,"data":view_data(&app,&rows.into_iter().collect())?}),
    ))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Input {
    mode: Mode,
    revision: String,
}
pub async fn save(
    State(app): State<App>,
    user: User,
    Json(input): Json<Input>,
) -> Result<Json<Value>> {
    let mut tx = app.pool.begin().await?;
    // Take the SQLite write lock before reading, so concurrent channel changes cannot be lost.
    sqlx::query("UPDATE users SET username=username WHERE id=?")
        .bind(&user.id)
        .execute(&mut *tx)
        .await?;
    let rows: Vec<(String, String)> = sqlx::query_as(
        "SELECT key,value FROM settings WHERE user_id=? AND key IN ('integrations','interaction')",
    )
    .bind(&user.id)
    .fetch_all(&mut *tx)
    .await?;
    let mut state: Snapshot = rows.into_iter().collect();
    if revision(&state) != input.revision {
        return Err(Error::Conflict(
            "设置刚刚有变化，请刷新后再选择相处方式".into(),
        ));
    }
    let (mut integrations, _) = decode(&app, &state)?;
    let mut cfg: crate::qq::Config =
        serde_json::from_value(integrations.get("qq").cloned().unwrap_or(json!({})))
            .map_err(|_| invalid("QQ 配置损坏"))?;
    let (paragraphs, delay, round, day, cooldown, hour) = input.mode.limits();
    let style = crate::qq::dialogue::Style {
        enabled: true,
        max_messages: paragraphs,
        interval_secs: 2,
    };
    style.validate()?;
    let tune = |f: &mut crate::qq::dialogue::Followup| {
        f.delay_minutes = delay;
        f.max_followups = round;
        f.daily_limit = day;
    };
    cfg.reply_style = style.clone();
    tune(&mut cfg.followup);
    cfg.followup.validate()?;
    for id in &cfg.groups {
        let g = cfg.group_settings.entry(id.clone()).or_default();
        g.reply_style = style.clone();
        tune(&mut g.followup);
        g.decision.limits_enabled = true;
        g.decision.cooldown_secs = cooldown;
        g.decision.max_replies_per_hour = hour;
        g.validate()?;
    }
    // Only these existing rhythm fields change; credentials, permissions and feature opt-ins stay intact.
    if integrations.get("qq").is_none() {
        integrations["qq"] = json!({});
    }
    integrations["qq"]["reply_style"] = json!(cfg.reply_style);
    integrations["qq"]["followup"] = json!(cfg.followup);
    integrations["qq"]["group_settings"] = json!(cfg.group_settings);
    let saved = Saved {
        mode: input.mode,
        fingerprint: digest(&controlled(&cfg).to_string()),
    };
    state.insert(
        "integrations".into(),
        json!(app.secrets.encrypt(&integrations.to_string())?).to_string(),
    );
    state.insert("interaction".into(), json!(saved).to_string());
    for (key, value) in &state {
        sqlx::query("INSERT INTO settings(user_id,key,value) VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value").bind(&user.id).bind(key).bind(value).execute(&mut *tx).await?;
    }
    tx.commit().await?;
    Ok(Json(json!({"ok":true,"data":view_data(&app,&state)?})))
}

pub fn closing_message(text: &str) -> bool {
    let normalized = text
        .trim_matches(|c: char| c.is_whitespace() || "。.!！~～，,、".contains(c))
        .to_lowercase();
    matches!(
        normalized.as_str(),
        "好" | "好的"
            | "好哒"
            | "嗯"
            | "嗯嗯"
            | "收到"
            | "谢谢"
            | "谢啦"
            | "晚安"
            | "拜拜"
            | "先这样"
            | "我去忙了"
            | "不聊了"
            | "不用回复了"
            | "ok"
            | "thanks"
            | "good night"
    )
}
