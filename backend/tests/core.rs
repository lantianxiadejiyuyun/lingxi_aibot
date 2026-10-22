use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use http_body_util::BodyExt;
use lingxi::{auth, chat, db, error::Error, jobs, memory, records, settings, state::App};
use serde_json::{Value, json};
use tower::ServiceExt;

struct Fixture {
    app: App,
    _directory: tempfile::TempDir,
}
impl Fixture {
    async fn new() -> Self {
        let directory = tempfile::tempdir().unwrap();
        let pool = db::connect(&directory.path().join("test.sqlite3"))
            .await
            .unwrap();
        let app = App::new(
            pool,
            db::Secrets::from_key(&[7; 32]),
            directory.path().to_owned(),
            true,
        )
        .unwrap();
        Self {
            app,
            _directory: directory,
        }
    }
    async fn user(&self, name: &str) -> auth::User {
        auth::create_user(&self.app, name, "a-long-test-password".into(), false)
            .await
            .unwrap()
    }
    async fn request(
        &self,
        method: &str,
        path: &str,
        token: Option<&str>,
        body: Value,
    ) -> (StatusCode, Value) {
        let mut request = Request::builder()
            .method(method)
            .uri(path)
            .header("content-type", "application/json");
        if let Some(token) = token {
            request = request.header("authorization", format!("Bearer {token}"));
        }
        let response = lingxi::api::router(self.app.clone())
            .oneshot(request.body(Body::from(body.to_string())).unwrap())
            .await
            .unwrap();
        let status = response.status();
        let bytes = response.into_body().collect().await.unwrap().to_bytes();
        (
            status,
            serde_json::from_slice(&bytes)
                .unwrap_or(json!({"raw":String::from_utf8_lossy(&bytes)})),
        )
    }
}
fn note(title: &str) -> records::Input {
    records::Input {
        title: title.into(),
        content: "周末去杭州旅行，准备路线和住宿".into(),
        data: json!({}),
        status: "open".into(),
        due_at: None,
        revision: None,
    }
}

#[tokio::test]
async fn api_authentication_isolation_and_revocation() {
    let f = Fixture::new().await;
    let alice = f.user("alice").await;
    let bob = f.user("bob").await;
    let token = auth::issue_token(&f.app, &alice, "test", 3600)
        .await
        .unwrap();
    let other = auth::issue_token(&f.app, &bob, "test", 3600).await.unwrap();
    assert_eq!(
        f.request("GET", "/api/v2/memories", None, Value::Null)
            .await
            .0,
        StatusCode::UNAUTHORIZED
    );
    let (status, r) = f
        .request(
            "POST",
            "/api/v2/records/notes",
            Some(&token),
            json!(note("杭州计划")),
        )
        .await;
    assert_eq!(status, StatusCode::CREATED, "{r}");
    let path = format!(
        "/api/v2/records/notes/{}",
        r["data"]["id"].as_str().unwrap()
    );
    assert_eq!(
        f.request("GET", &path, Some(&other), Value::Null).await.0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        f.request("DELETE", &path, Some(&other), Value::Null)
            .await
            .0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        f.request(
            "POST",
            "/api/v2/users",
            Some(&token),
            json!({"username":"eve","password":"a-long-password"})
        )
        .await
        .0,
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        f.request("POST", "/api/v2/logout", Some(&token), json!({}))
            .await
            .0,
        StatusCode::OK
    );
    assert!(auth::resolve(&f.app, &token).await.is_err());
    let raw: Vec<String> = sqlx::query_scalar("SELECT hash FROM tokens")
        .fetch_all(&f.app.pool)
        .await
        .unwrap();
    assert!(!raw.contains(&other));
}
#[tokio::test]
async fn login_checks_password_and_limits_attempts() {
    let f = Fixture::new().await;
    f.user("alice").await;
    assert_eq!(
        f.request(
            "POST",
            "/api/v2/login",
            None,
            json!({"username":"alice","password":"wrong"})
        )
        .await
        .0,
        StatusCode::UNAUTHORIZED
    );
    let (status, body) = f
        .request(
            "POST",
            "/api/v2/login",
            None,
            json!({"username":"alice","password":"a-long-test-password"}),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    assert!(body["data"]["token"].is_string());
    for _ in 0..8 {
        let _ = f
            .request(
                "POST",
                "/api/v2/login",
                None,
                json!({"username":"alice","password":"wrong"}),
            )
            .await;
    }
    assert_eq!(
        f.request(
            "POST",
            "/api/v2/login",
            None,
            json!({"username":"alice","password":"wrong"})
        )
        .await
        .0,
        StatusCode::TOO_MANY_REQUESTS
    );
}
#[tokio::test]
async fn records_use_optimistic_updates_and_real_chinese_search() {
    let f = Fixture::new().await;
    let user = f.user("alice").await;
    let row = records::create(&f.app, &user, "notes", note("旅行计划"))
        .await
        .unwrap();
    let mut updated = note("已修改");
    updated.revision = Some(1);
    records::update(&f.app, &user, "notes", &row.id, updated.clone())
        .await
        .unwrap();
    assert!(matches!(
        records::update(&f.app, &user, "notes", &row.id, updated).await,
        Err(Error::Conflict(_))
    ));
    for query in ["杭州", "杭州旅行"] {
        let found = records::list(
            &f.app,
            &user,
            "notes",
            &records::List {
                q: Some(query.into()),
                ..Default::default()
            },
        )
        .await
        .unwrap();
        assert_eq!(found["items"].as_array().unwrap().len(), 1);
    }
    records::delete(&f.app, &user, "notes", &row.id)
        .await
        .unwrap();
    let found = records::list(
        &f.app,
        &user,
        "notes",
        &records::List {
            q: Some("杭州旅行".into()),
            ..Default::default()
        },
    )
    .await
    .unwrap();
    assert_eq!(found["items"], json!([]));
}
#[tokio::test]
async fn expense_money_and_dates_are_validated() {
    let f = Fixture::new().await;
    let user = f.user("alice").await;
    let mut input = note("午餐");
    input.data = json!({"amount_cents":12.34,"date":"2026-10-21"});
    assert!(
        records::create(&f.app, &user, "expenses", input.clone())
            .await
            .is_err()
    );
    input.data = json!({"amount_cents":1234,"date":"2026-02-30"});
    assert!(
        records::create(&f.app, &user, "expenses", input.clone())
            .await
            .is_err()
    );
    input.data = json!({"amount_cents":1234,"date":"2026-10-21"});
    assert_eq!(
        records::create(&f.app, &user, "expenses", input)
            .await
            .unwrap()
            .data["amount_cents"],
        1234
    );
}
#[tokio::test]
async fn memory_preserves_manual_facts_and_deleted_tombstones() {
    let f = Fixture::new().await;
    let alice = f.user("alice").await;
    let bob = f.user("bob").await;
    let fact = memory::Input {
        content: "用户偏好简短回答".into(),
        importance: 5,
        expires_at: None,
    };
    memory::remember(&f.app, &alice, &fact, "manual", &json!([]))
        .await
        .unwrap();
    let auto = memory::Input {
        importance: 1,
        ..fact
    };
    memory::remember(&f.app, &alice, &auto, "auto", &json!([]))
        .await
        .unwrap();
    let items = memory::list(&f.app, &alice).await.unwrap();
    assert_eq!(items.len(), 1);
    assert_eq!(items[0].importance, 5);
    assert_eq!(items[0].source, "manual");
    assert!(memory::list(&f.app, &bob).await.unwrap().is_empty());
    memory::forget(&f.app, &alice, &items[0].id).await.unwrap();
    memory::remember(&f.app, &alice, &auto, "auto", &json!([]))
        .await
        .unwrap();
    assert!(memory::list(&f.app, &alice).await.unwrap().is_empty());
    memory::remember(&f.app, &alice, &auto, "manual", &json!([]))
        .await
        .unwrap();
    assert_eq!(memory::list(&f.app, &alice).await.unwrap().len(), 1);
    let expired = memory::Input {
        content: "旧的一次性安排".into(),
        importance: 5,
        expires_at: Some(db::now() - 1),
    };
    memory::remember(&f.app, &alice, &expired, "manual", &json!([]))
        .await
        .unwrap();
    assert_eq!(memory::list(&f.app, &alice).await.unwrap().len(), 1);
}
#[tokio::test]
async fn concurrent_workers_claim_one_job_and_recover_expired_lease() {
    let f = Fixture::new().await;
    let user = f.user("alice").await;
    let input = jobs::Input {
        action: "notify".into(),
        params: json!({"body":"hello"}),
        run_at: None,
        request_id: Some("once".into()),
    };
    let job = jobs::enqueue(&f.app, &user, input.clone()).await.unwrap();
    assert_eq!(
        jobs::enqueue(&f.app, &user, input).await.unwrap().id,
        job.id
    );
    let (a, b) = tokio::join!(
        jobs::claim(&f.app, "worker-a"),
        jobs::claim(&f.app, "worker-b")
    );
    assert_eq!(a.unwrap().is_some() as u8 + b.unwrap().is_some() as u8, 1);
    sqlx::query("UPDATE jobs SET lease_until=? WHERE id=?")
        .bind(db::now() - 1)
        .bind(&job.id)
        .execute(&f.app.pool)
        .await
        .unwrap();
    let recovered = jobs::claim(&f.app, "worker-c").await.unwrap().unwrap();
    assert_eq!(recovered.attempts, 2);
}
#[tokio::test]
async fn side_effectful_jobs_are_not_blindly_repeated() {
    let f = Fixture::new().await;
    let user = f.user("alice").await;
    let job = jobs::enqueue(
        &f.app,
        &user,
        jobs::Input {
            action: "webhook".into(),
            params: json!({}),
            run_at: None,
            request_id: None,
        },
    )
    .await
    .unwrap();
    jobs::claim(&f.app, "worker-a").await.unwrap();
    sqlx::query("UPDATE jobs SET lease_until=? WHERE id=?")
        .bind(db::now() - 1)
        .bind(&job.id)
        .execute(&f.app.pool)
        .await
        .unwrap();
    assert!(jobs::claim(&f.app, "worker-b").await.unwrap().is_none());
    assert_eq!(
        jobs::get(&f.app, &user, &job.id).await.unwrap().status,
        "needs_review"
    );
}
#[tokio::test]
async fn schedule_dispatch_is_deduplicated() {
    let f = Fixture::new().await;
    let user = f.user("alice").await;
    let sid = jobs::add_schedule(
        &f.app,
        &user,
        jobs::ScheduleInput {
            name: "daily".into(),
            action: "notify".into(),
            params: json!({}),
            cron: "0 4 * * *".into(),
            timezone: "Asia/Shanghai".into(),
            enabled: true,
        },
    )
    .await
    .unwrap();
    sqlx::query("UPDATE schedules SET next_run_at=? WHERE id=?")
        .bind(db::now() - 1)
        .bind(sid)
        .execute(&f.app.pool)
        .await
        .unwrap();
    let (a, b) = tokio::join!(jobs::tick_schedules(&f.app), jobs::tick_schedules(&f.app));
    a.unwrap();
    b.unwrap();
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM jobs")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(count, 1);
    let next = jobs::next_run("0 4 * * *", "Asia/Shanghai", 1_792_512_000).unwrap();
    assert!(next > 1_792_512_000);
    assert!(jobs::next_run("bad", "Asia/Shanghai", db::now()).is_err());
}
#[tokio::test]
async fn conversation_leases_exclude_other_processes_and_users() {
    let f = Fixture::new().await;
    let alice = f.user("alice").await;
    let bob = f.user("bob").await;
    let c = chat::create(&f.app, &alice, "test", None).await.unwrap();
    let lease = chat::Lease::acquire(&f.app, &alice, &c.id).await.unwrap();
    assert!(chat::Lease::acquire(&f.app, &alice, &c.id).await.is_err());
    assert!(chat::Lease::acquire(&f.app, &bob, &c.id).await.is_err());
    lease.check().await.unwrap();
    sqlx::query("UPDATE conversations SET lease_until=? WHERE id=?")
        .bind(db::now() - 1)
        .bind(&c.id)
        .execute(&f.app.pool)
        .await
        .unwrap();
    let replacement = chat::Lease::acquire(&f.app, &alice, &c.id).await.unwrap();
    assert!(lease.check().await.is_err());
    replacement.check().await.unwrap();
}
#[test]
fn stream_decoder_preserves_utf8_and_split_tool_arguments() {
    let text = concat!(
        "data: {\"choices\":[{\"delta\":{\"content\":\"你好🌿\"}}]}\r\n\r\n",
        "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"c1\",\"function\":{\"name\":\"remember\",\"arguments\":\"{\\\"content\\\":\"}}]}}]}\n\n",
        "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"function\":{\"arguments\":\"\\\"hello\\\"}\"}}]}}]}\n\n",
        "data: [DONE]\n\n"
    );
    let mut decoder = lingxi::provider::Decoder::default();
    for bytes in text.as_bytes().chunks(3) {
        decoder.feed(bytes, "openai").unwrap();
    }
    let reply = decoder.finish().unwrap();
    assert_eq!(reply.text, "你好🌿");
    assert_eq!(reply.calls[0].arguments["content"], "hello");
    let mut broken = lingxi::provider::Decoder::default();
    broken
        .feed(
            b"data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\n",
            "openai",
        )
        .unwrap();
    assert!(broken.finish().is_err());
}
#[test]
fn anthropic_stream_decoder_handles_text_and_tools() {
    let frames = [
        json!({"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你好"}}),
        json!({"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"tool1","name":"remember","input":{}}}),
        json!({"type":"content_block_delta","index":1,"delta":{"partial_json":"{\"content\":\"喜欢茶\"}"}}),
        json!({"type":"message_stop"}),
    ];
    let mut decoder = lingxi::provider::Decoder::default();
    for frame in frames {
        decoder
            .feed(format!("data: {frame}\n\n").as_bytes(), "anthropic")
            .unwrap();
    }
    let reply = decoder.finish().unwrap();
    assert_eq!(reply.text, "你好");
    assert_eq!(reply.calls[0].arguments["content"], "喜欢茶");
}
#[tokio::test]
async fn credential_encryption_masks_roundtrip_and_rejects_wrong_key() {
    let f = Fixture::new().await;
    let user = f.user("alice").await;
    let profile = settings::Profile {
        id: "test".into(),
        name: "test".into(),
        protocol: "openai".into(),
        base_url: "http://127.0.0.1:9/v1".into(),
        model: "mock".into(),
        api_key: "secret-api-key".into(),
        context_window: 32000,
        max_output: 4096,
        system_prompt: String::new(),
    };
    settings::save_profiles(&f.app, &user, vec![profile])
        .await
        .unwrap();
    let raw = settings::get(&f.app, &user, "profiles")
        .await
        .unwrap()
        .unwrap()
        .to_string();
    assert!(!raw.contains("secret-api-key"));
    let masked = settings::profiles(&f.app, &user, false).await.unwrap();
    assert_eq!(masked[0].api_key, "********");
    settings::save_profiles(&f.app, &user, masked)
        .await
        .unwrap();
    assert_eq!(
        settings::profiles(&f.app, &user, true).await.unwrap()[0].api_key,
        "secret-api-key"
    );
    let cipher = f.app.secrets.encrypt("test").unwrap();
    assert!(db::Secrets::from_key(&[8; 32]).decrypt(&cipher).is_err());
}
#[tokio::test]
async fn storage_rejects_path_traversal_and_non_admin_registration() {
    let f = Fixture::new().await;
    let mut user = f.user("alice").await;
    let cfg = json!({"storage":[{"id":"local","name":"local","path":f._directory.path()}]});
    assert!(
        lingxi::integrations::save_config(&f.app, &user, cfg.clone())
            .await
            .is_err()
    );
    user.is_admin = true;
    lingxi::integrations::save_config(&f.app, &user, cfg)
        .await
        .unwrap();
    assert!(
        lingxi::media::storage_path(&f.app, &user, "local", "../outside")
            .await
            .is_err()
    );
    assert!(
        lingxi::media::storage_path(&f.app, &user, "local", "")
            .await
            .is_ok()
    );
}
#[tokio::test]
async fn legacy_import_is_atomic_and_archives_all_original_rows() {
    let f = Fixture::new().await;
    let snapshot = json!({"format":"lingxi-legacy-v1","tables":{"users":[{"id":1,"username":"owner","is_admin":true}],"notes":[{"id":1,"user_id":1,"title":"旧笔记","content":"完整正文"}],"some_retired_table":[{"id":1,"secret":"sensitive"}]}});
    let report = lingxi::migration::import(
        &f.app,
        &serde_json::to_vec(&snapshot).unwrap(),
        "new-import-password".into(),
    )
    .await
    .unwrap();
    assert_eq!(report["archived_rows"], 3);
    assert_eq!(report["imported_rows"], 1);
    let archive: String = sqlx::query_scalar(
        "SELECT ciphertext FROM legacy_archive WHERE table_name='some_retired_table'",
    )
    .fetch_one(&f.app.pool)
    .await
    .unwrap();
    assert!(!archive.contains("sensitive"));
    assert!(
        f.app
            .secrets
            .decrypt(&archive)
            .unwrap()
            .contains("sensitive")
    );
    assert!(
        lingxi::migration::import(
            &f.app,
            &serde_json::to_vec(&snapshot).unwrap(),
            "new-import-password".into()
        )
        .await
        .is_err()
    );
    let other = Fixture::new().await;
    let mut bad = snapshot;
    bad["tables"]["notes"][0]["user_id"] = json!(99);
    assert!(
        lingxi::migration::import(
            &other.app,
            &serde_json::to_vec(&bad).unwrap(),
            "new-import-password".into()
        )
        .await
        .is_err()
    );
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM users")
        .fetch_one(&other.app.pool)
        .await
        .unwrap();
    assert_eq!(count, 0);
}
