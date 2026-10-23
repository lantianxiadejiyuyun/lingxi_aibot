use crate::{
    auth::User,
    error::{Result, invalid},
    state::App,
};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

pub fn definitions() -> Vec<Value> {
    let entries = vec![
        (
            "read_bilibili",
            "按主人要求提交B站视频字幕阅读。video为BV号或完整视频链接，留空按已保存兴趣发现视频。后台完成后到B站书房查看；没有字幕不能声称看过。不会点赞评论或发送私信。",
            json!({"video":{"type":"string"}}),
            vec![],
        ),
        (
            "bilibili_notes",
            "读取当前主人最近实际读过的B站视频字幕心得，含可核实链接。仅有字幕，不代表看过画面。",
            json!({}),
            vec![],
        ),
        (
            "list_records",
            "查询当前用户的任务/笔记/日程/消费/健身/出行/网页/书签。kind: tasks,notes,events,expenses,fitness,trips,pages,bookmarks。",
            json!({"kind":{"type":"string"},"query":{"type":"string"}}),
            vec!["kind"],
        ),
        (
            "get_record",
            "读取一条业务记录的完整内容与 revision。",
            json!({"kind":{"type":"string"},"id":{"type":"string"}}),
            vec!["kind", "id"],
        ),
        (
            "save_record",
            "创建或更新业务记录。title 必填，content 为正文，data 为结构化字段。任务 data.priority=1–3；日程 data.start_at/end_at=UTC Unix秒；消费 data.amount_cents=分且data.date=YYYY-MM-DD；健身date/minutes；出行start_date/end_date。更新时必须携带 id 和原 revision。",
            json!({"kind":{"type":"string"},"id":{"type":"string"},"title":{"type":"string"},"content":{"type":"string"},"data":{"type":"object"},"status":{"type":"string"},"due_at":{"type":"integer"},"revision":{"type":"integer"}}),
            vec!["kind", "title"],
        ),
        (
            "delete_record",
            "按用户明确要求删除一条记录。",
            json!({"kind":{"type":"string"},"id":{"type":"string"}}),
            vec!["kind", "id"],
        ),
        (
            "remember",
            "按用户要求保存一条长期事实或偏好。",
            json!({"content":{"type":"string"},"importance":{"type":"integer","minimum":1,"maximum":5},"expires_at":{"type":["integer","null"]}}),
            vec!["content"],
        ),
        (
            "list_memories",
            "查看当前用户的长期记忆。",
            json!({}),
            vec![],
        ),
        (
            "forget",
            "按用户明确要求遗忘一条长期记忆。",
            json!({"id":{"type":"string"}}),
            vec!["id"],
        ),
        (
            "search_history",
            "搜索笔记、网页、聊天和长期记忆，返回有来源的片段。",
            json!({"query":{"type":"string"}}),
            vec!["query"],
        ),
        (
            "schedule",
            "创建按用户时区运行的计划。action 为 notify/reminders/consolidate/briefing，cron 为5字段表达式，params 为动作参数。notify 参数 title/body。",
            json!({"name":{"type":"string"},"action":{"type":"string"},"cron":{"type":"string"},"params":{"type":"object"}}),
            vec!["name", "action", "cron", "params"],
        ),
        (
            "web_search",
            "用用户配置的搜索服务进行联网检索。",
            json!({"query":{"type":"string"}}),
            vec!["query"],
        ),
        (
            "fetch_url",
            "读取用户指定或搜索结果中的网页，最多返回12000字符。",
            json!({"url":{"type":"string"}}),
            vec!["url"],
        ),
        (
            "generate_image",
            "提交后台图片生成任务，prompt 为画面描述。",
            json!({"prompt":{"type":"string"}}),
            vec!["prompt"],
        ),
        (
            "list_storage",
            "列出用户配置的存储位置。",
            json!({}),
            vec![],
        ),
        (
            "list_files",
            "浏览已授权存储中的相对路径。",
            json!({"storage_id":{"type":"string"},"path":{"type":"string"}}),
            vec!["storage_id"],
        ),
        (
            "download",
            "提交 aria2 或 qBittorrent 后台下载。url 必须来自用户或可核实的搜索结果。",
            json!({"url":{"type":"string"},"storage_id":{"type":"string"},"downloader":{"type":"string","enum":["aria2","qbittorrent"]}}),
            vec!["url", "storage_id", "downloader"],
        ),
        (
            "job_status",
            "查看后台任务的真实状态。",
            json!({"id":{"type":"string"}}),
            vec!["id"],
        ),
    ];
    entries.into_iter().map(|(name,description,properties,required)|json!({"type":"function","function":{"name":name,"description":description,"parameters":{"type":"object","properties":properties,"required":required,"additionalProperties":false}}})).collect()
}
fn field<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value[key]
        .as_str()
        .filter(|v| !v.is_empty())
        .ok_or_else(|| invalid(format!("{key} 必填")))
}
fn feature(name: &str) -> Option<crate::permissions::Feature> {
    use crate::permissions::Feature;
    match name {
        "remember" | "list_memories" | "forget" | "search_history" => Some(Feature::Memory),
        "read_bilibili" | "bilibili_notes" => Some(Feature::Bilibili),
        _ => None,
    }
}
pub async fn permitted_definitions(app: &App, user: &User) -> Result<Vec<Value>> {
    use crate::permissions::{Feature, Scope};
    let access = crate::permissions::config(app, user).await?;
    if !access.allows(Feature::Tools, Scope::Private) {
        return Ok(vec![]);
    }
    Ok(definitions()
        .into_iter()
        .filter(|v| {
            feature(v["function"]["name"].as_str().unwrap_or(""))
                .is_none_or(|f| access.allows(f, Scope::Private))
        })
        .collect())
}
pub async fn execute(
    app: &App,
    user: &User,
    name: &str,
    mut args: Value,
    cancel: &CancellationToken,
) -> Result<Value> {
    use crate::permissions::{Feature, Scope};
    crate::permissions::require(app, user, Feature::Tools, Scope::Private).await?;
    if let Some(feature) = feature(name) {
        crate::permissions::require(app, user, feature, Scope::Private).await?;
    }
    if cancel.is_cancelled() {
        return Err(crate::error::Error::Cancelled);
    }
    if !args.is_object() {
        return Err(invalid("工具参数必须是对象"));
    }
    match name {
        "read_bilibili" => {
            crate::bilibili::queue(
                app,
                user,
                crate::bilibili::Input {
                    video: args["video"].as_str().unwrap_or("").into(),
                    retry: false,
                    request_id: crate::db::id(),
                },
                false,
            )
            .await
        }
        "bilibili_notes" => Ok(json!(
            crate::bilibili::companion_sources(app, user, crate::db::now()).await?
        )),
        "list_records" => {
            crate::records::list(
                app,
                user,
                field(&args, "kind")?,
                &crate::records::List {
                    q: args["query"].as_str().map(String::from),
                    limit: Some(20),
                    ..Default::default()
                },
            )
            .await
        }
        "get_record" => Ok(json!(
            crate::records::get(app, user, field(&args, "kind")?, field(&args, "id")?).await?
        )),
        "save_record" => {
            let kind = field(&args, "kind")?.to_owned();
            let rid = args["id"].as_str().map(String::from);
            if ["skills", "images"].contains(&kind.as_str()) {
                return Err(invalid("该资源须通过专用管理入口创建"));
            }
            args.as_object_mut().unwrap().remove("kind");
            args.as_object_mut().unwrap().remove("id");
            if kind == "pages" {
                args["data"]["public"] = json!(false);
            }
            let input = serde_json::from_value(args).map_err(|e| invalid(e.to_string()))?;
            Ok(json!(if let Some(id) = rid {
                crate::records::update(app, user, &kind, &id, input).await?
            } else {
                crate::records::create(app, user, &kind, input).await?
            }))
        }
        "delete_record" => {
            crate::records::delete(app, user, field(&args, "kind")?, field(&args, "id")?).await?;
            Ok(json!({"deleted":true}))
        }
        "remember" => {
            let input = serde_json::from_value(args).map_err(|e| invalid(e.to_string()))?;
            crate::memory::remember(app, user, &input, "manual", &json!([])).await?;
            Ok(json!({"remembered":true}))
        }
        "list_memories" => Ok(json!(crate::memory::list(app, user).await?)),
        "forget" => {
            crate::memory::forget(app, user, field(&args, "id")?).await?;
            Ok(json!({"forgotten":true}))
        }
        "search_history" => crate::search::search(app, user, field(&args, "query")?).await,
        "schedule" => {
            if !["notify", "reminders", "consolidate", "briefing"]
                .contains(&field(&args, "action")?)
            {
                return Err(invalid("AI 计划动作不支持该类型"));
            }
            let sid = crate::jobs::add_schedule(
                app,
                user,
                crate::jobs::ScheduleInput {
                    name: field(&args, "name")?.into(),
                    action: field(&args, "action")?.into(),
                    params: args["params"].clone(),
                    cron: field(&args, "cron")?.into(),
                    timezone: user.timezone.clone(),
                    enabled: true,
                },
            )
            .await?;
            Ok(json!({"id":sid}))
        }
        "web_search" => crate::integrations::web_search(app, user, field(&args, "query")?).await,
        "fetch_url" => crate::integrations::fetch_url(app, field(&args, "url")?).await,
        "generate_image" | "download" => {
            let action = if name == "generate_image" {
                "image"
            } else {
                "download"
            };
            Ok(json!(
                crate::jobs::enqueue(
                    app,
                    user,
                    crate::jobs::Input {
                        action: action.into(),
                        params: args,
                        run_at: None,
                        request_id: None
                    }
                )
                .await?
            ))
        }
        "list_storage" => crate::media::list_storage(app, user).await,
        "list_files" => {
            crate::media::list_files(
                app,
                user,
                field(&args, "storage_id")?,
                args["path"].as_str().unwrap_or(""),
            )
            .await
        }
        "job_status" => Ok(json!(
            crate::jobs::get(app, user, field(&args, "id")?).await?
        )),
        _ => Err(invalid("工具不存在或未授权")),
    }
}
