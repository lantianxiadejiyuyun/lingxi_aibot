use axum::{
    Json,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde_json::json;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("{0}")]
    Invalid(String),
    #[error("请先登录或提供有效 Token")]
    Unauthorized,
    #[error("没有权限执行此操作")]
    Forbidden,
    #[error("记录不存在")]
    NotFound,
    #[error("{0}")]
    Conflict(String),
    #[error("请求过于频繁，请稍后重试")]
    Busy,
    #[error("{0}")]
    Provider(String),
    #[error("请求已取消")]
    Cancelled,
    #[error(transparent)]
    Database(#[from] sqlx::Error),
    #[error(transparent)]
    Internal(#[from] anyhow::Error),
}
pub type Result<T> = std::result::Result<T, Error>;
impl IntoResponse for Error {
    fn into_response(self) -> Response {
        let (status, code, message) = match &self {
            Self::Invalid(s) => (StatusCode::BAD_REQUEST, "invalid_request", s.clone()),
            Self::Unauthorized => (StatusCode::UNAUTHORIZED, "unauthorized", self.to_string()),
            Self::Forbidden => (StatusCode::FORBIDDEN, "forbidden", self.to_string()),
            Self::NotFound => (StatusCode::NOT_FOUND, "not_found", self.to_string()),
            Self::Conflict(s) => (StatusCode::CONFLICT, "conflict", s.clone()),
            Self::Busy => (StatusCode::TOO_MANY_REQUESTS, "busy", self.to_string()),
            Self::Provider(s) => (StatusCode::BAD_GATEWAY, "provider_error", s.clone()),
            Self::Cancelled => (StatusCode::CONFLICT, "cancelled", self.to_string()),
            _ => {
                tracing::error!(error=%self,"request failed");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "internal_error",
                    "服务暂时不可用".into(),
                )
            }
        };
        (
            status,
            Json(json!({"ok":false,"code":code,"error":message})),
        )
            .into_response()
    }
}
pub fn invalid(message: impl Into<String>) -> Error {
    Error::Invalid(message.into())
}
