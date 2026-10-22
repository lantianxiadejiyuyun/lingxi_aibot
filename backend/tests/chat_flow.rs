use axum::{
    Json, Router,
    body::Body,
    extract::State,
    http::{Response, header},
    routing::post,
};
use lingxi::{auth, chat, db, memory, settings, state::App};
use serde_json::{Value, json};
use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};
use tokio_util::sync::CancellationToken;

#[derive(Clone)]
struct Mock {
    calls: Arc<AtomicUsize>,
    requests: Arc<Mutex<Vec<Value>>>,
    mode: &'static str,
}
async fn model(State(mock): State<Mock>, Json(request): Json<Value>) -> Response<Body> {
    mock.requests.lock().unwrap().push(request.clone());
    let index = mock.calls.fetch_add(1, Ordering::SeqCst);
    let content = match mock.mode {
        "tools" if index == 0 => {
            json!({"choices":[{"delta":{"tool_calls":[{"index":0,"id":"save-one","function":{"name":"save_record","arguments":json!({"kind":"notes","title":"旅行清单","content":"带好雨伞"}).to_string()}}]}}]})
        }
        "summary" => {
            json!({"choices":[{"delta":{"content":"用户决定周末去杭州，偏好简洁回答。"}}]})
        }
        "invalid_summary" => json!({"choices":[{"delta":{"content":"没有缩短".repeat(3000)}}]}),
        "invalid_memory" => json!({"choices":[{"delta":{"content":"这不是JSON"}}]}),
        "cancel" => json!({"choices":[{"delta":{"content":"已生成部分"}}]}),
        _ => json!({"choices":[{"delta":{"content":"已记下旅行清单。"}}]}),
    };
    let stream = async_stream::stream! {
     yield Ok::<_,std::io::Error>(format!("data: {content}\n\n"));
     if mock.mode=="cancel"{tokio::time::sleep(Duration::from_secs(5)).await;}
     yield Ok::<_,std::io::Error>("data: [DONE]\n\n".to_owned());
    };
    Response::builder()
        .header(header::CONTENT_TYPE, "text/event-stream")
        .body(Body::from_stream(stream))
        .unwrap()
}
struct Fixture {
    app: App,
    user: auth::User,
    profile: settings::Profile,
    mock: Mock,
    server: tokio::task::JoinHandle<()>,
    _temp: tempfile::TempDir,
}
impl Fixture {
    async fn new(mode: &'static str) -> Self {
        let temp = tempfile::tempdir().unwrap();
        let pool = db::connect(&temp.path().join("test.sqlite3"))
            .await
            .unwrap();
        let app = App::new(
            pool,
            db::Secrets::from_key(&[9; 32]),
            temp.path().into(),
            true,
        )
        .unwrap();
        let user = auth::create_user(&app, "alice", "test-model-password".into(), false)
            .await
            .unwrap();
        let mock = Mock {
            calls: Arc::new(AtomicUsize::new(0)),
            requests: Default::default(),
            mode,
        };
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let router = Router::new()
            .route("/v1/chat/completions", post(model))
            .with_state(mock.clone());
        let server = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        let profile = settings::Profile {
            id: "mock".into(),
            name: "Mock".into(),
            protocol: "openai".into(),
            base_url: format!("http://{address}/v1"),
            model: "mock".into(),
            api_key: "not-a-real-secret".into(),
            context_window: 32000,
            max_output: 4096,
            system_prompt: String::new(),
        };
        settings::save_profiles(&app, &user, vec![profile.clone()])
            .await
            .unwrap();
        Self {
            app,
            user,
            profile,
            mock,
            server,
            _temp: temp,
        }
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        self.server.abort();
    }
}
#[tokio::test]
async fn multi_round_tools_are_persisted_and_request_replay_has_no_side_effects() {
    let f = Fixture::new("tools").await;
    let conversation = chat::create(&f.app, &f.user, "旅行", None).await.unwrap();
    let input = chat::Send {
        message: "帮我记下旅行清单".into(),
        request_id: "message-once".into(),
    };
    let session = chat::prepare(&f.app, &f.user, &conversation.id, input.clone())
        .await
        .unwrap();
    let reply = chat::run(f.app.clone(), session, None, CancellationToken::new())
        .await
        .unwrap();
    assert_eq!(reply, "已记下旅行清单。");
    let session = chat::prepare(&f.app, &f.user, &conversation.id, input)
        .await
        .unwrap();
    assert!(session.replay.is_some());
    assert_eq!(
        chat::run(f.app.clone(), session, None, CancellationToken::new())
            .await
            .unwrap(),
        reply
    );
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM records WHERE kind='notes'")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(count, 1);
    let messages = chat::messages(&f.app, &f.user, &conversation.id, 0, 100)
        .await
        .unwrap();
    assert_eq!(messages.len(), 2);
    assert_eq!(messages[1].content, reply);
    let steps: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM tool_steps WHERE status='completed'")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(steps, 1);
    assert_eq!(f.mock.calls.load(Ordering::SeqCst), 2);
    let requests = f.mock.requests.lock().unwrap();
    assert!(
        requests[1]["messages"]
            .as_array()
            .unwrap()
            .iter()
            .any(|m| m["role"] == "tool")
    );
}
#[tokio::test]
async fn cancellation_retains_partial_text_and_marks_turn_interrupted() {
    let f = Fixture::new("cancel").await;
    let c = chat::create(&f.app, &f.user, "取消测试", None)
        .await
        .unwrap();
    let session = chat::prepare(
        &f.app,
        &f.user,
        &c.id,
        chat::Send {
            message: "开始".into(),
            request_id: "cancel-once".into(),
        },
    )
    .await
    .unwrap();
    let cancel = CancellationToken::new();
    let (tx, mut rx) = tokio::sync::mpsc::channel(32);
    let app = f.app.clone();
    let cloned = cancel.clone();
    let task = tokio::spawn(async move { chat::run(app, session, Some(tx), cloned).await });
    while let Some(event) = rx.recv().await {
        if event.kind == "delta" {
            assert_eq!(event.data, "已生成部分");
            cancel.cancel();
            break;
        }
    }
    assert!(task.await.unwrap().is_err());
    let messages = chat::messages(&f.app, &f.user, &c.id, 0, 100)
        .await
        .unwrap();
    assert_eq!(messages.last().unwrap().content, "已生成部分");
    let status: String = sqlx::query_scalar("SELECT status FROM turns")
        .fetch_one(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(status, "interrupted");
}
async fn history(f: &Fixture, cid: &str) {
    for i in 0..24 {
        sqlx::query(
            "INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)",
        )
        .bind(cid)
        .bind(if i % 2 == 0 { "user" } else { "assistant" })
        .bind(format!(
            "第{i}条：{}",
            "周末去杭州旅行，保留详细计划。".repeat(20)
        ))
        .bind(db::now())
        .execute(&f.app.pool)
        .await
        .unwrap();
    }
}
#[tokio::test]
async fn compaction_keeps_raw_history_and_commits_boundary_with_summary() {
    let f = Fixture::new("summary").await;
    let c = chat::create(&f.app, &f.user, "长对话", None).await.unwrap();
    history(&f, &c.id).await;
    let lease = chat::Lease::acquire(&f.app, &f.user, &c.id).await.unwrap();
    assert!(
        chat::compact(
            &f.app,
            &f.user,
            &c.id,
            &f.profile,
            &lease,
            true,
            &CancellationToken::new()
        )
        .await
        .unwrap()
    );
    let c = chat::conversation(&f.app, &f.user, &c.id).await.unwrap();
    assert!(c.through_id > 0);
    assert!(c.summary.contains("杭州"));
    assert_eq!(
        chat::messages(&f.app, &f.user, &c.id, 0, 100)
            .await
            .unwrap()
            .len(),
        24
    );
    let context = chat::build_context(&f.app, &f.user, &c.id, &f.profile, "继续")
        .await
        .unwrap();
    assert!(context.len() < 24);
    assert!(
        context
            .iter()
            .any(|m| m["role"] == "system" && m["content"].as_str().unwrap().contains("历史摘要"))
    );
}
#[tokio::test]
async fn failed_summary_and_invalid_memory_output_preserve_existing_state() {
    let f = Fixture::new("invalid_summary").await;
    let c = chat::create(&f.app, &f.user, "长对话", None).await.unwrap();
    history(&f, &c.id).await;
    let lease = chat::Lease::acquire(&f.app, &f.user, &c.id).await.unwrap();
    assert!(
        chat::compact(
            &f.app,
            &f.user,
            &c.id,
            &f.profile,
            &lease,
            true,
            &CancellationToken::new()
        )
        .await
        .is_err()
    );
    let c = chat::conversation(&f.app, &f.user, &c.id).await.unwrap();
    assert_eq!(c.through_id, 0);
    assert!(c.summary.is_empty());
    let g = Fixture::new("invalid_memory").await;
    let c = chat::create(&g.app, &g.user, "记忆", None).await.unwrap();
    history(&g, &c.id).await;
    memory::remember(
        &g.app,
        &g.user,
        &memory::Input {
            content: "重要的既有事实".into(),
            importance: 5,
            expires_at: None,
        },
        "auto",
        &json!([]),
    )
    .await
    .unwrap();
    assert!(
        memory::extract(&g.app, &g.user, &g.profile, &CancellationToken::new())
            .await
            .is_err()
    );
    assert_eq!(
        memory::list(&g.app, &g.user).await.unwrap()[0].content,
        "重要的既有事实"
    );
}
