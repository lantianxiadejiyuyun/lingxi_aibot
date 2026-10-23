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
        ..Default::default()
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
        ("POST", "/api/v2/deployment/napcat/login"),
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
    assert_eq!(info["data"]["napcat"]["login_available"], false);
    assert_eq!(
        request(
            &app,
            "POST",
            "/api/v2/deployment/napcat/login",
            Some(&token)
        )
        .await
        .0,
        StatusCode::BAD_REQUEST
    );
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

#[tokio::test]
async fn napcat_session_handoff_keeps_master_key_server_side_and_observes_rotation_and_2fa() {
    use std::sync::{Arc, Mutex};
    let calls = Arc::new(Mutex::new(Vec::<Value>::new()));
    let result = Arc::new(Mutex::new(
        json!({"code":0,"data":{"Credential":"synthetic-short-session"}}),
    ));
    let capture = calls.clone();
    let output = result.clone();
    let mock = axum::Router::new().route(
        "/api/auth/login",
        axum::routing::post(move |axum::Json(input): axum::Json<Value>| {
            capture.lock().unwrap().push(input);
            let output = output.lock().unwrap().clone();
            async move { axum::Json(output) }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("http://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move { axum::serve(listener, mock).await.unwrap() });
    let dir = tempfile::tempdir().unwrap();
    let config = dir.path().join("webui.json");
    let master = "synthetic-private-key-with-特殊&=chars";
    tokio::fs::write(&config, json!({"token":master}).to_string())
        .await
        .unwrap();
    let mut app = App::new(
        db::memory().await.unwrap(),
        db::Secrets::from_key(&[4; 32]),
        dir.path().into(),
        true,
    )
    .unwrap();
    app.deployment = Config {
        napcat_managed: true,
        napcat_webui_url: Some("https://nas.example.com/webui/".into()),
        napcat_config_dir: Some(dir.path().into()),
        napcat_webui_internal_url: Some(endpoint),
        ..Default::default()
    };
    let user = auth::create_user(&app, "admin", "long-test-password".into(), true)
        .await
        .unwrap();
    let token = auth::issue_token(&app, &user, "test", 3600).await.unwrap();
    let (_, info) = request(&app, "GET", "/api/v2/deployment", Some(&token)).await;
    assert_eq!(info["data"]["napcat"]["login_available"], true);
    assert!(!info.to_string().contains(master));
    assert!(
        calls.lock().unwrap().is_empty(),
        "Discovery must not start a login"
    );
    let res = lingxi::api::router(app.clone())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v2/deployment/napcat/login")
                .header("authorization", format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::OK);
    assert_eq!(res.headers()["cache-control"], "no-store");
    let bytes = res.into_body().collect().await.unwrap().to_bytes();
    let data: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(
        data["data"]["url"],
        "https://nas.example.com/webui/lingxi-login/"
    );
    assert_eq!(data["data"]["credential"], "synthetic-short-session");
    assert!(!data.to_string().contains(master));
    assert_eq!(
        calls.lock().unwrap()[0],
        json!({"hash":db::digest(&format!("{master}.napcat"))})
    );

    let rotated = "rotated-private-key";
    let new_file = dir.path().join("new.json");
    tokio::fs::write(&new_file, json!({"token":rotated}).to_string())
        .await
        .unwrap();
    tokio::fs::rename(new_file, &config).await.unwrap();
    app.deployment.napcat_webui_url = Some("https://nas.example.com/prefix/webui/web_login".into());
    let (status, data) = request(
        &app,
        "POST",
        "/api/v2/deployment/napcat/login",
        Some(&token),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        data["data"]["url"],
        "https://nas.example.com/prefix/webui/lingxi-login/"
    );
    assert_eq!(
        calls.lock().unwrap()[1],
        json!({"hash":db::digest(&format!("{rotated}.napcat"))})
    );

    *result.lock().unwrap() = json!({"code":0,"data":{"require2FA":true}});
    let (status, data) = request(
        &app,
        "POST",
        "/api/v2/deployment/napcat/login",
        Some(&token),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(data["error"].as_str().unwrap().contains("二次验证"));
    *result.lock().unwrap() = json!({"code":-1,"message":rotated});
    let (status, data) = request(
        &app,
        "POST",
        "/api/v2/deployment/napcat/login",
        Some(&token),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_GATEWAY);
    assert!(
        !data.to_string().contains(rotated),
        "Never forward raw upstream error bodies"
    );
    app.deployment.napcat_managed = false;
    let count = calls.lock().unwrap().len();
    assert_eq!(
        request(
            &app,
            "POST",
            "/api/v2/deployment/napcat/login",
            Some(&token)
        )
        .await
        .0,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(calls.lock().unwrap().len(), count);
    app.pool.close().await;
    server.abort();
}

#[tokio::test]
async fn unavailable_or_invalid_napcat_config_never_exposes_a_key_or_calls_upstream() {
    let dir = tempfile::tempdir().unwrap();
    let mut app = App::new(
        db::memory().await.unwrap(),
        db::Secrets::from_key(&[5; 32]),
        dir.path().into(),
        true,
    )
    .unwrap();
    app.deployment = Config {
        napcat_managed: true,
        napcat_webui_url: Some("http://127.0.0.1:6099/webui".into()),
        napcat_webui_internal_url: Some("http://127.0.0.1:1".into()),
        napcat_config_dir: Some(dir.path().into()),
        ..Default::default()
    };
    let user = auth::create_user(&app, "admin", "long-test-password".into(), true)
        .await
        .unwrap();
    let token = auth::issue_token(&app, &user, "test", 3600).await.unwrap();
    for contents in [
        "invalid-json".to_string(),
        json!({"token":""}).to_string(),
        json!({"token":"x".repeat(1025)}).to_string(),
        "x".repeat(65_537),
    ] {
        tokio::fs::write(dir.path().join("webui.json"), contents)
            .await
            .unwrap();
        let (_, info) = request(&app, "GET", "/api/v2/deployment", Some(&token)).await;
        assert_eq!(info["data"]["napcat"]["login_available"], false);
        assert_eq!(
            request(
                &app,
                "POST",
                "/api/v2/deployment/napcat/login",
                Some(&token)
            )
            .await
            .0,
            StatusCode::BAD_REQUEST
        );
    }
    app.pool.close().await;
}
