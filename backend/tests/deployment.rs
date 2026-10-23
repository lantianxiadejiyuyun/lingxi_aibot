use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use http_body_util::BodyExt;
use lingxi::{auth, db, deployment::Config, integrations, state::App};
use serde_json::{Value, json};
use tower::ServiceExt;

async fn request(app: &App, method: &str, path: &str, token: Option<&str>) -> (StatusCode, Value) {
    let mut req = Request::builder().method(method).uri(path);
    if let Some(token) = token {
        req = req.header("authorization", format!("Bearer {token}"));
    }
    let res = lingxi::api::router(app.clone())
        .oneshot(req.body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = res.status();
    let data = res.into_body().collect().await.unwrap().to_bytes();
    (status, serde_json::from_slice(&data).unwrap())
}

#[tokio::test]
async fn deployment_is_admin_only_and_managed_setup_preserves_identity_and_secrets() {
    let dir = tempfile::tempdir().unwrap();
    let mut app = App::new(
        db::memory().await.unwrap(),
        db::Secrets::from_key(&[3; 32]),
        dir.path().into(),
        true,
    )
    .unwrap();
    let secret = "managed-napcat-secret-with-over-32-characters";
    app.feishu_embedded = true;
    app.deployment = Config {
        public_url: Some("https://bot.example.com".into()),
        napcat_webui_url: Some("http://192.168.1.2:6099/webui".into()),
        napcat_http_url: Some("http://127.0.0.1:3000".into()),
        napcat_managed: true,
        napcat_http_token: Some(secret.into()),
    };
    let admin = auth::create_user(&app, "admin", "long-test-password".into(), true)
        .await
        .unwrap();
    let user = auth::create_user(&app, "member", "long-test-password".into(), false)
        .await
        .unwrap();
    let token = auth::issue_token(&app, &admin, "test", 3600).await.unwrap();
    let other = auth::issue_token(&app, &user, "test", 3600).await.unwrap();
    for (method, path) in [
        ("GET", "/api/v2/deployment"),
        ("POST", "/api/v2/deployment/napcat/configure"),
    ] {
        assert_eq!(
            request(&app, method, path, None).await.0,
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            request(&app, method, path, Some(&other)).await.0,
            StatusCode::FORBIDDEN
        );
    }
    let (status, info) = request(&app, "GET", "/api/v2/deployment", Some(&token)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        info["data"]["napcat"]["callback_url"],
        format!("https://bot.example.com/api/v2/qq/inbound/{}", admin.id)
    );
    assert_eq!(
        info["data"]["napcat"]["internal_callback_url"],
        format!("http://app:8000/api/v2/qq/inbound/{}", admin.id)
    );
    assert_eq!(info["data"]["feishu"]["managed"], true);
    assert!(!info.to_string().contains(secret));
    let previous = json!({"qq":{"owner_qq":"12345","bot_qq":"67890","groups":["99999"],"enabled":false,"private_enabled":false,"onebot":{"url":"http://127.0.0.1:4000","token":"old"},"inbound":{"secret":"existing-callback-secret-preserved"}},"untouched":{"token":"different-secret"}});
    integrations::save_config(&app, &admin, previous.clone())
        .await
        .unwrap();
    assert_eq!(
        request(
            &app,
            "POST",
            "/api/v2/deployment/napcat/configure",
            Some(&token)
        )
        .await
        .0,
        StatusCode::OK
    );
    let saved = integrations::config(&app, &admin).await.unwrap();
    for key in [
        "owner_qq",
        "bot_qq",
        "groups",
        "enabled",
        "private_enabled",
        "inbound",
    ] {
        assert_eq!(saved["qq"][key], previous["qq"][key]);
    }
    assert_eq!(saved["untouched"], previous["untouched"]);
    assert_eq!(saved["qq"]["onebot"]["token"], secret);
    assert_eq!(saved["qq"]["onebot"]["url"], "http://127.0.0.1:3000");
    assert_eq!(integrations::config(&app, &user).await.unwrap(), json!({}));
    let (_, redacted) = request(&app, "GET", "/api/v2/integrations", Some(&token)).await;
    assert!(!redacted.to_string().contains(secret));
    assert_eq!(redacted["data"]["qq"]["onebot"]["token"], "********");
    app.pool.close().await;
}

#[tokio::test]
async fn missing_deployment_does_not_invent_addresses_or_modify_config() {
    let dir = tempfile::tempdir().unwrap();
    let app = App::new(
        db::memory().await.unwrap(),
        db::Secrets::from_key(&[2; 32]),
        dir.path().into(),
        true,
    )
    .unwrap();
    let user = auth::create_user(&app, "admin", "long-test-password".into(), true)
        .await
        .unwrap();
    let token = auth::issue_token(&app, &user, "test", 3600).await.unwrap();
    let (_, info) = request(&app, "GET", "/api/v2/deployment", Some(&token)).await;
    assert!(info["data"]["napcat"]["webui_url"].is_null());
    assert_eq!(info["data"]["napcat"]["configure_available"], false);
    assert_eq!(
        request(
            &app,
            "POST",
            "/api/v2/deployment/napcat/configure",
            Some(&token)
        )
        .await
        .0,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(integrations::config(&app, &user).await.unwrap(), json!({}));
    app.pool.close().await;
}
