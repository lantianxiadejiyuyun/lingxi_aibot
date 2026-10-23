//! Bounded retries for read-only vision calls. Never retries a chat turn or delivery.
use crate::{
    auth::User,
    error::{Error, Result, invalid},
    permissions::{self, Feature, Scope},
    settings::Profile,
    state::App,
};
use serde::de::DeserializeOwned;
use serde_json::{Value, json};
use std::time::Duration;
use tokio_util::sync::CancellationToken;

pub(crate) struct Failure {
    pub error: Error,
    pub code: &'static str,
    pub attempts: u8,
}

impl Failure {
    pub fn message(&self) -> &'static str {
        match self.code {
            "permission" => "图片识别权限已关闭，未识别内容",
            "authentication" => "看图接口认证失败，请检查模型 API Key；未识别内容",
            "request" => {
                "当前看图模型未能识别这张图片：接口拒绝图片请求，请检查模型和接口配置；未识别内容"
            }
            "context" => "看图模型的上下文窗口不足以容纳图片，请调大窗口或更换模型；未识别内容",
            "timeout" => "看图请求超时，重试后仍未成功；未识别内容",
            "format" => "看图模型的说明格式不完整，重试后仍无法读取；未识别内容",
            "output_limit" => "看图模型输出被截断，请提高输出预算；未识别内容",
            "rate_limit" => "看图接口限流，重试后仍未成功，请稍后再试；未识别内容",
            _ => "看图服务暂时未能完成识别，重试后仍未成功；未识别内容",
        }
    }
}

fn classify(error: &Error) -> (&'static str, bool) {
    match error {
        Error::Cancelled => ("cancelled", false),
        Error::Forbidden => ("permission", false),
        Error::Provider(s) if s.contains("HTTP 401") || s.contains("HTTP 403") => {
            ("authentication", false)
        }
        Error::Provider(s) if s.contains("HTTP 429") => ("rate_limit", true),
        Error::Provider(s) if s.contains("HTTP 408") || s.contains("超时") => ("timeout", true),
        Error::Provider(s) if s.contains("HTTP 5") => ("upstream", true),
        Error::Provider(s) if s.contains("HTTP 4") => ("request", false),
        Error::Provider(s) | Error::Invalid(s) if s.contains("上下文") => ("context", false),
        Error::Provider(s) if s.contains("输出达到长度限制") => ("output_limit", true),
        Error::Provider(s)
            if s.contains("中断")
                || s.contains("空内容")
                || s.contains("JSON 无效")
                || s.contains("返回错误") =>
        {
            ("upstream", true)
        }
        _ => ("request", false),
    }
}

/// Accept one complete JSON object, optionally fenced or introduced by a short sentence.
/// Reject truncated objects and multiple answers; never treat arbitrary prose as an observation.
pub(crate) fn parse_json<T: DeserializeOwned>(raw: &str) -> Result<T> {
    if raw.len() > 16_384 {
        return Err(invalid("看图模型返回内容过长"));
    }
    let raw = crate::memory::strip_json(raw);
    let start = raw
        .find('{')
        .ok_or_else(|| invalid("看图模型未返回 JSON 对象"))?;
    if raw[..start].chars().count() > 256 {
        return Err(invalid("看图模型返回格式无效"));
    }
    let mut stream = serde_json::Deserializer::from_str(&raw[start..]).into_iter::<T>();
    let value = stream
        .next()
        .ok_or_else(|| invalid("看图模型返回空内容"))?
        .map_err(|_| invalid("看图模型未返回有效 JSON"))?;
    let tail = &raw[start + stream.byte_offset()..];
    if tail.chars().count() > 256 || tail.contains(['{', '}']) {
        return Err(invalid("看图模型返回多个或不完整的结果"));
    }
    Ok(value)
}

pub(crate) async fn query<T>(
    app: &App,
    user: &User,
    scope: Scope,
    collection: bool,
    profile: &Profile,
    prompt: &str,
    content: &[Value],
    cancel: &CancellationToken,
    parse: impl Fn(&str) -> Result<T>,
) -> std::result::Result<(T, u8), Failure> {
    let mut profile = profile.clone();
    // Some vision models spend output tokens on reasoning before emitting JSON.
    // Preserve the configured ceiling, but don't force every model down to 2048.
    profile.max_output = profile.max_output.min(4096);
    for attempt in 1..=2 {
        let permitted = async {
            permissions::require(app, user, Feature::Vision, scope).await?;
            if collection {
                permissions::require(app, user, Feature::StickerCollection, scope).await?;
            }
            Ok(())
        };
        if let Err(error) = permitted.await {
            return Err(Failure {
                error,
                code: "permission",
                attempts: attempt - 1,
            });
        }
        if cancel.is_cancelled() || app.shutdown.is_cancelled() {
            return Err(Failure {
                error: Error::Cancelled,
                code: "cancelled",
                attempts: attempt - 1,
            });
        }
        let system = if attempt == 2 {
            format!(
                "{prompt}\n请直接输出一个完整、简短的 JSON 对象，不加前言、分析或 Markdown 代码块。"
            )
        } else {
            prompt.to_owned()
        };
        let messages = [
            json!({"role":"system","content":system}),
            json!({"role":"user","content":content}),
        ];
        let mut attempt_profile = profile.clone();
        let input_budget = crate::provider::token_estimate(&json!(messages))
            + crate::provider::token_estimate(&json!([]));
        let available = profile.context_window.saturating_sub(input_budget);
        if available < 256 {
            return Err(Failure {
                error: invalid("图片超过模型上下文预算，请选择更大窗口"),
                code: "context",
                attempts: attempt - 1,
            });
        }
        attempt_profile.max_output = attempt_profile.max_output.min(available);
        let reply = tokio::select! {
            _ = cancel.cancelled() => Err(Error::Cancelled),
            _ = app.shutdown.cancelled() => Err(Error::Cancelled),
            result = tokio::time::timeout(Duration::from_secs(45), crate::provider::chat(app, &attempt_profile, &messages, &[], None, cancel, None)) => {
                result.unwrap_or_else(|_| Err(Error::Provider("看图请求超时".into())))
            }
        };
        let (error, code, retry) = match reply {
            Ok(reply) => {
                let parsed = if reply.calls.is_empty() {
                    parse(&reply.text)
                } else {
                    Err(invalid("看图模型不应返回工具调用"))
                };
                match parsed {
                    Ok(value) => return Ok((value, attempt)),
                    Err(error) => (error, "format", true),
                }
            }
            Err(error) => {
                let (code, retry) = classify(&error);
                (error, code, retry)
            }
        };
        // Do not log model output, signed image URLs, API keys or image contents.
        tracing::warn!(
            stage = "vision",
            code,
            attempt,
            retry = retry && attempt < 2,
            "QQ image observation failed"
        );
        if !retry || attempt == 2 {
            return Err(Failure {
                error,
                code,
                attempts: attempt,
            });
        }
        tokio::select! {
            _ = cancel.cancelled() => return Err(Failure { error: Error::Cancelled, code: "cancelled", attempts: attempt }),
            _ = app.shutdown.cancelled() => return Err(Failure { error: Error::Cancelled, code: "cancelled", attempts: attempt }),
            _ = tokio::time::sleep(Duration::from_millis(500)) => (),
        }
    }
    unreachable!()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn complete_json_wrappers_are_tolerated_but_not_truncation_or_multiple_objects() {
        for raw in [
            r#"{"description":"花 { 蓝色 }"}"#,
            "```JSON\n{\"description\":\"花\"}\n```",
            "观察结果：\n{\"description\":\"花\"}",
        ] {
            assert!(parse_json::<Value>(raw).is_ok());
        }
        for raw in ["{\"description\":\"花", "{} {}", "只有描述，没有 JSON"] {
            assert!(parse_json::<Value>(raw).is_err());
        }
    }
}
