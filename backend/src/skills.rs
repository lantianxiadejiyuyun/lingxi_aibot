use crate::{
    auth::User,
    error::{Result, invalid},
    state::App,
};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;
pub async fn run(
    app: &App,
    user: &User,
    skill_id: &str,
    cancel: &CancellationToken,
) -> Result<Value> {
    let skill = crate::records::get(app, user, "skills", skill_id).await?;
    if skill.data["enabled"] != true {
        return Err(invalid("技能尚未审阅启用"));
    }
    let steps = skill.data["steps"]
        .as_array()
        .ok_or_else(|| invalid("技能缺少步骤"))?;
    let definitions = crate::tools::definitions();
    if steps.len() > 16
        || steps.iter().any(|s| {
            !definitions
                .iter()
                .any(|d| d["function"]["name"] == s["tool"])
        })
    {
        return Err(invalid("技能包含未知工具或超过16步"));
    }
    let mut results = Vec::new();
    for step in steps {
        results.push(
            crate::tools::execute(
                app,
                user,
                step["tool"].as_str().unwrap_or(""),
                step["arguments"].clone(),
                cancel,
            )
            .await?,
        );
    }
    Ok(json!({"results":results}))
}
