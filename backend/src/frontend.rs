use axum::{
    extract::Path,
    http::{StatusCode, header},
    response::{IntoResponse, Response},
};

include!(concat!(env!("OUT_DIR"), "/frontend.rs"));

pub async fn index() -> Response {
    respond("index.html")
}
pub async fn static_asset(Path(path): Path<String>) -> Response {
    respond(&format!("assets/{path}"))
}
fn respond(path: &str) -> Response {
    match asset(path) {
        Some((mime, bytes)) => ([(header::CONTENT_TYPE, mime)], bytes).into_response(),
        None => StatusCode::NOT_FOUND.into_response(),
    }
}
