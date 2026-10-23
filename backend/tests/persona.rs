use axum::{
    Json, Router,
    body::Body,
    extract::{Path, State},
    http::{Response, header},
    routing::post,
};
use lingxi::{auth, chat, db, growth, settings, state::App};
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};
use tokio_util::sync::CancellationToken;

#[derive(Clone, Default)]
struct Mock {
    requests: Arc<Mutex<Vec<Value>>>,
    mode: Arc<Mutex<String>>,
    started: Arc<tokio::sync::Notify>,
    resume: Arc<tokio::sync::Notify>,
}
async fn model(State(mock): State<Mock>, Json(request): Json<Value>) -> Response<Body> {
    mock.requests.lock().unwrap().push(request.clone());
    let mode = mock.mode.lock().unwrap().clone();
    let content = if request["messages"][0]["content"]
        .as_str()
        .unwrap_or("")
        .contains("养成记录员")
    {
        if mode == "wait" {
            mock.started.notify_one();
            mock.resume.notified().await;
        }
        let source: Value =
            serde_json::from_str(request["messages"][1]["content"].as_str().unwrap()).unwrap();
        let count = source["owner_messages"].as_array().unwrap().len();
        let reflections = mock
            .requests
            .lock()
            .unwrap()
            .iter()
            .filter(|r| {
                r["messages"][0]["content"]
                    .as_str()
                    .unwrap_or("")
                    .contains("养成记录员")
            })
            .count();
        if (mode == "truncate-large" && count > 3)
            || (mode == "fail-after-first" && reflections > 1)
            || (mode == "needs-output-budget" && request["max_tokens"].as_u64().unwrap() < 4096)
        {
            let chunk = json!({"choices":[{"delta":{"content":"["},"finish_reason":"length"}]});
            return Response::builder()
                .header(header::CONTENT_TYPE, "text/event-stream")
                .body(Body::from(format!("data: {chunk}\n\ndata: [DONE]\n\n")))
                .unwrap();
        }
        if mode == "skip" {
            "[]".into()
        } else if mode.starts_with("temperament") {
            let first = &source["owner_messages"][0]["message_id"];
            let second = if mode == "temperament-distinct" {
                &source["owner_messages"][1]["message_id"]
            } else {
                first
            };
            json!([{"kind":"temperament","name":"轻松表达","content":"轻松聊天时慢慢形成简洁直白的表达习惯。","evidence":[{"message_id":first,"quote":"喜欢你用短句聊天"},{"message_id":second,"quote":"喜欢你用短句聊天"}]}]).to_string()
        } else {
            let mid = &source["owner_messages"][0]["message_id"];
            json!([{"kind":"habit","name":"短句聊天","content":"主人喜欢简洁、轻松的短句，不需要动作旁白。","evidence":[{"message_id":mid,"quote":if mode=="invalid"{"凭空编造的原话"}else{"喜欢你用短句聊天"}}]}]).to_string()
        }
    } else {
        "记住啦，慢慢聊。".into()
    };
    let chunk = json!({"choices":[{"delta":{"content":content}}]});
    Response::builder()
        .header(header::CONTENT_TYPE, "text/event-stream")
        .body(Body::from(format!("data: {chunk}\n\ndata: [DONE]\n\n")))
        .unwrap()
}
struct Fixture {
    app: App,
    user: auth::User,
    profile: settings::Profile,
    cid: String,
    mock: Mock,
    server: tokio::task::JoinHandle<()>,
    _temp: tempfile::TempDir,
}
impl Fixture {
    async fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let app = App::new(
            db::connect(&temp.path().join("main.sqlite3"))
                .await
                .unwrap(),
            db::Secrets::from_key(&[6; 32]),
            temp.path().into(),
            true,
        )
        .unwrap();
        let user = auth::create_user(&app, "alice", "growth-test-password".into(), false)
            .await
            .unwrap();
        let mock = Mock::default();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let router = Router::new()
            .route("/v1/chat/completions", post(model))
            .with_state(mock.clone());
        let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        let profile = settings::Profile {
            id: "fish".into(),
            name: "大肥鱼".into(),
            protocol: "openai".into(),
            base_url: format!("http://{addr}/v1"),
            model: "mock".into(),
            api_key: String::new(),
            context_window: 32000,
            max_output: 2048,
            system_prompt: "住在晚潮湾，甜甜的，带一点傲娇和腹黑，不输出动作旁白。".into(),
        };
        settings::save_profiles(&app, &user, vec![profile.clone()])
            .await
            .unwrap();
        let cid = chat::create(&app, &user, "私聊", Some(profile.id.clone()))
            .await
            .unwrap()
            .id;
        Self {
            app,
            user,
            profile,
            cid,
            mock,
            server,
            _temp: temp,
        }
    }
    async fn view(&self) -> Value {
        growth::view(
            State(self.app.clone()),
            self.user.clone(),
            Path(self.profile.id.clone()),
        )
        .await
        .unwrap()
        .0["data"]
            .clone()
    }
    async fn enable(&self, value: bool) {
        let revision = self.view().await["revision"].as_i64().unwrap();
        let _ = growth::save(
            State(self.app.clone()),
            self.user.clone(),
            Path(self.profile.id.clone()),
            Json(growth::Config {
                enabled: value,
                revision,
            }),
        )
        .await
        .unwrap();
    }
    async fn chat(&self, key: &str) {
        let s = chat::prepare(
            &self.app,
            &self.user,
            &self.cid,
            chat::Send {
                message: "我喜欢你用短句聊天，不需要动作旁白。".into(),
                request_id: key.into(),
            },
        )
        .await
        .unwrap();
        chat::run(self.app.clone(), s, None, CancellationToken::new())
            .await
            .unwrap();
    }
    async fn six(&self) {
        for _ in 0..6 {
            self.chat(&db::id()).await;
        }
    }
    async fn reflect(&self) -> lingxi::error::Result<Value> {
        growth::reflect(
            &self.app,
            &self.user,
            &json!({"profile_id":self.profile.id}),
            &CancellationToken::new(),
        )
        .await
    }
    async fn reset_clock(&self) {
        sqlx::query("UPDATE persona_growth SET next_at=0")
            .execute(&self.app.pool)
            .await
            .unwrap();
    }
    async fn manual(&self) {
        let _ = growth::write_trait(
            State(self.app.clone()),
            self.user.clone(),
            Path(self.profile.id.clone()),
            Json(growth::Input {
                kind: "habit".into(),
                name: "短句聊天".into(),
                content: "只在轻松聊天时用短句，认真问题请解释清楚。".into(),
                locked: true,
            }),
        )
        .await
        .unwrap();
    }
    fn reflections(&self) -> Vec<Value> {
        self.mock
            .requests
            .lock()
            .unwrap()
            .iter()
            .filter(|r| {
                r["messages"][0]["content"]
                    .as_str()
                    .unwrap_or("")
                    .contains("养成记录员")
            })
            .cloned()
            .collect()
    }
    async fn queue(&self, request: &str) -> lingxi::error::Result<Value> {
        growth::reflect_now(
            State(self.app.clone()),
            self.user.clone(),
            Path(self.profile.id.clone()),
            Json(growth::Manual {
                revision: self.view().await["revision"].as_i64().unwrap(),
                request_id: request.into(),
            }),
        )
        .await
        .map(|v| v.0["data"].clone())
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        self.server.abort();
    }
}

#[tokio::test]
async fn temperament_requires_feedback_from_two_distinct_messages() {
    let f = Fixture::new().await;
    f.enable(true).await;
    f.six().await;
    *f.mock.mode.lock().unwrap() = "temperament-duplicate".into();
    assert!(f.reflect().await.is_err());
    assert!(f.view().await["traits"].as_array().unwrap().is_empty());
    f.reset_clock().await;
    *f.mock.mode.lock().unwrap() = "temperament-distinct".into();
    assert_eq!(f.reflect().await.unwrap()["changed"], 1);
    assert_eq!(f.view().await["traits"][0]["kind"], "temperament");
}

#[tokio::test]
async fn growth_is_opt_in_bounded_sourced_and_used_in_private_context() {
    let f = Fixture::new().await;
    f.chat("before-enabled").await;
    assert_eq!(f.view().await["turns"], 0);
    f.enable(true).await;
    f.chat("one").await;
    f.chat("one").await;
    assert_eq!(f.view().await["turns"], 1);
    f.chat("job:background-research").await;
    assert_eq!(f.view().await["turns"], 1);
    assert_eq!(f.reflect().await.unwrap()["skipped"], true);
    for _ in 0..5 {
        f.chat(&db::id()).await;
    }
    growth::reconcile(&f.app).await.unwrap();
    growth::reconcile(&f.app).await.unwrap();
    let queued: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM jobs WHERE action='persona_reflect'")
            .fetch_one(&f.app.pool)
            .await
            .unwrap();
    assert_eq!(queued, 1);
    assert_eq!(f.reflect().await.unwrap()["changed"], 1);
    let v = f.view().await;
    assert_eq!(v["pending"], 0);
    assert_eq!(v["traits"][0]["source"], "auto");
    assert_eq!(v["traits"][0]["evidence"][0]["quote"], "喜欢你用短句聊天");
    let prompt = chat::build_context(&f.app, &f.user, &f.cid, &f.profile, "hi")
        .await
        .unwrap();
    assert!(json!(prompt).to_string().contains("私人相处中形成"));
    assert_eq!(
        settings::active_profile(&f.app, &f.user, None)
            .await
            .unwrap()
            .system_prompt,
        f.profile.system_prompt
    );
    assert!(!f.app.data_dir.join("qq/people.sqlite3").exists());
    f.six().await;
    assert_eq!(f.reflect().await.unwrap()["skipped"], true); // two-hour cooldown
    f.reset_clock().await;
    *f.mock.mode.lock().unwrap() = "skip".into();
    assert_eq!(f.reflect().await.unwrap()["changed"], 0);
    assert_eq!(f.view().await["traits"].as_array().unwrap().len(), 1);
}
#[tokio::test]
async fn manual_locks_and_deleted_keys_survive_automatic_reflection() {
    let f = Fixture::new().await;
    f.enable(true).await;
    f.manual().await;
    f.six().await;
    assert_eq!(f.reflect().await.unwrap()["changed"], 0);
    let t = f.view().await["traits"][0].clone();
    assert_eq!(t["source"], "manual");
    let _ = growth::forget(
        State(f.app.clone()),
        f.user.clone(),
        Path((f.profile.id.clone(), t["id"].as_str().unwrap().into())),
    )
    .await
    .unwrap();
    f.six().await;
    f.reset_clock().await;
    assert_eq!(f.reflect().await.unwrap()["changed"], 0);
    assert!(f.view().await["traits"].as_array().unwrap().is_empty());
    f.manual().await;
    assert_eq!(f.view().await["traits"].as_array().unwrap().len(), 1);
}
#[tokio::test]
async fn invented_evidence_is_rejected_without_advancing_or_replacing_growth() {
    let f = Fixture::new().await;
    f.enable(true).await;
    f.manual().await;
    f.six().await;
    *f.mock.mode.lock().unwrap() = "invalid".into();
    assert!(f.reflect().await.is_err());
    let v = f.view().await;
    assert_eq!(v["pending"], 6);
    assert_eq!(v["traits"][0]["source"], "manual");
    assert!(
        v["last_error"]
            .as_str()
            .unwrap()
            .contains("成长来源无法验证")
    );
}
#[tokio::test]
async fn disabling_during_generation_cancels_writes_and_profiles_are_isolated() {
    let f = Fixture::new().await;
    f.enable(true).await;
    f.six().await;
    *f.mock.mode.lock().unwrap() = "wait".into();
    let a = f.app.clone();
    let u = f.user.clone();
    let task = tokio::spawn(async move {
        growth::reflect(
            &a,
            &u,
            &json!({"profile_id":"fish"}),
            &CancellationToken::new(),
        )
        .await
        .unwrap()
    });
    f.mock.started.notified().await;
    assert_eq!(f.reflect().await.unwrap()["skipped"], true);
    f.enable(false).await;
    f.mock.resume.notify_one();
    assert_eq!(task.await.unwrap()["cancelled"], "settings_changed");
    assert!(f.view().await["traits"].as_array().unwrap().is_empty());
    f.manual().await;
    assert!(
        growth::context(&f.app, &f.user, "another-persona")
            .await
            .unwrap()
            .is_empty()
    );
    let other = auth::create_user(&f.app, "bob", "other-growth-password".into(), false)
        .await
        .unwrap();
    assert!(
        growth::context(&f.app, &other, "fish")
            .await
            .unwrap()
            .is_empty()
    );
    assert!(
        growth::view(State(f.app.clone()), other, Path("fish".into()))
            .await
            .is_err()
    );
    let request = f
        .mock
        .requests
        .lock()
        .unwrap()
        .iter()
        .find(|r| {
            r["messages"][0]["content"]
                .as_str()
                .unwrap_or("")
                .contains("养成记录员")
        })
        .unwrap()
        .clone();
    let material: Value =
        serde_json::from_str(request["messages"][1]["content"].as_str().unwrap()).unwrap();
    assert_eq!(material["owner_messages"].as_array().unwrap().len(), 6);
    assert!(material.get("assistant_messages").is_none());
}

#[tokio::test]
async fn twenty_two_observations_are_processed_in_four_batches_using_profile_output_budget() {
    let f = Fixture::new().await;
    let mut profile = f.profile.clone();
    profile.max_output = 8192;
    settings::save_profiles(&f.app, &f.user, vec![profile])
        .await
        .unwrap();
    f.enable(true).await;
    for _ in 0..22 {
        f.chat(&db::id()).await;
    }
    *f.mock.mode.lock().unwrap() = "needs-output-budget".into();
    let result = f.reflect().await.unwrap();
    assert_eq!(result["processed"], 22);
    assert_eq!(f.view().await["pending"], 0);
    let requests = f.reflections();
    let sizes: Vec<usize> = requests
        .iter()
        .map(|r| {
            assert_eq!(r["max_tokens"], 8192);
            serde_json::from_str::<Value>(r["messages"][1]["content"].as_str().unwrap())
                    .unwrap()["owner_messages"]
                    .as_array()
                    .unwrap()
                    .len()
        })
        .collect();
    assert_eq!(sizes, vec![6, 6, 6, 4]);
    assert!(f.view().await["next_at"].as_i64().unwrap() >= db::now() + 7199);
}

#[tokio::test]
async fn truncated_batch_shrinks_without_skipping_observations() {
    let f = Fixture::new().await;
    f.enable(true).await;
    f.six().await;
    f.six().await;
    *f.mock.mode.lock().unwrap() = "truncate-large".into();
    assert_eq!(f.reflect().await.unwrap()["processed"], 12);
    assert_eq!(f.view().await["pending"], 0);
    let sizes: Vec<usize> = f
        .reflections()
        .iter()
        .map(|r| {
            serde_json::from_str::<Value>(r["messages"][1]["content"].as_str().unwrap())
                    .unwrap()["owner_messages"]
                    .as_array()
                    .unwrap()
                    .len()
        })
        .collect();
    assert_eq!(sizes, vec![6, 3, 3, 3, 3]);
}

#[tokio::test]
async fn failed_later_batch_keeps_prior_progress_and_can_resume_manually() {
    let f = Fixture::new().await;
    f.enable(true).await;
    f.six().await;
    f.six().await;
    *f.mock.mode.lock().unwrap() = "fail-after-first".into();
    assert!(f.reflect().await.is_err());
    assert_eq!(f.view().await["pending"], 6);
    assert_eq!(f.view().await["traits"].as_array().unwrap().len(), 1);
    *f.mock.mode.lock().unwrap() = "skip".into();
    let queued = f.queue("resume").await.unwrap();
    let job = lingxi::jobs::get(&f.app, &f.user, queued["job_id"].as_str().unwrap())
        .await
        .unwrap();
    let result = growth::reflect(&f.app, &f.user, &job.params.0, &CancellationToken::new())
        .await
        .unwrap();
    assert_eq!(result["processed"], 6);
    assert_eq!(result["changed"], 0);
    assert_eq!(f.view().await["pending"], 0);
    assert_eq!(f.view().await["last_error"], "");
}

#[tokio::test]
async fn small_context_splits_material_before_sending_to_provider() {
    let f = Fixture::new().await;
    f.enable(true).await;
    f.six().await;
    sqlx::query("UPDATE messages SET content=? WHERE role='user'")
        .bind("长".repeat(1000))
        .execute(&f.app.pool)
        .await
        .unwrap();
    let mut p = f.profile.clone();
    p.context_window = 8192;
    settings::save_profiles(&f.app, &f.user, vec![p])
        .await
        .unwrap();
    *f.mock.mode.lock().unwrap() = "skip".into();
    assert_eq!(f.reflect().await.unwrap()["processed"], 6);
    assert_eq!(f.view().await["pending"], 0);
    assert!(f.reflections().len() > 1);
}

#[tokio::test]
async fn manual_reflection_bypasses_cadence_and_deduplicates_concurrent_requests() {
    let f = Fixture::new().await;
    f.enable(true).await;
    f.chat("one").await;
    sqlx::query("UPDATE persona_growth SET next_at=?")
        .bind(db::now() + 7200)
        .execute(&f.app.pool)
        .await
        .unwrap();
    assert_eq!(f.reflect().await.unwrap()["skipped"], true);
    let (a, b) = tokio::join!(f.queue("click-a"), f.queue("click-b"));
    let a = a.unwrap();
    let b = b.unwrap();
    assert_eq!(a["job_id"], b["job_id"]);
    assert_eq!(f.view().await["job_status"], "queued");
    let job = lingxi::jobs::get(&f.app, &f.user, a["job_id"].as_str().unwrap())
        .await
        .unwrap();
    assert_eq!(
        growth::reflect(&f.app, &f.user, &job.params.0, &CancellationToken::new())
            .await
            .unwrap()["processed"],
        1
    );
    sqlx::query("UPDATE jobs SET status='completed' WHERE id=?")
        .bind(&job.id)
        .execute(&f.app.pool)
        .await
        .unwrap();
    let first_request = if f.queue("click-a").await.is_ok() {
        "click-a"
    } else {
        "click-b"
    };
    assert_eq!(f.queue(first_request).await.unwrap()["job_id"], job.id);
    assert!(f.queue("empty").await.is_err());
}

#[tokio::test]
async fn manual_reflection_requires_opt_in_permission_and_current_revision() {
    let f = Fixture::new().await;
    assert!(f.queue("disabled").await.is_err());
    f.enable(true).await;
    f.chat("one").await;
    let stale = growth::reflect_now(
        State(f.app.clone()),
        f.user.clone(),
        Path("fish".into()),
        Json(growth::Manual {
            revision: 0,
            request_id: "stale".into(),
        }),
    )
    .await;
    assert!(matches!(stale, Err(lingxi::error::Error::Conflict(_))));
    let queued = f.queue("then-disabled").await.unwrap();
    let job = lingxi::jobs::get(&f.app, &f.user, queued["job_id"].as_str().unwrap())
        .await
        .unwrap();
    f.enable(false).await;
    assert_eq!(
        growth::reflect(&f.app, &f.user, &job.params.0, &CancellationToken::new())
            .await
            .unwrap()["skipped"],
        true
    );
    f.enable(true).await;
    f.chat("two").await;
    let mut cfg = lingxi::permissions::Config::default();
    cfg.features
        .get_mut(&lingxi::permissions::Feature::PersonaGrowth)
        .unwrap()
        .private = false;
    settings::put(&f.app, &f.user, "feature_permissions", &json!(cfg))
        .await
        .unwrap();
    assert!(
        matches!(f.queue("forbidden").await, Err(lingxi::error::Error::Invalid(message)) if message.contains("权限已关闭"))
    );
    assert_eq!(f.view().await["can_reflect"], false);
    assert!(f.reflections().is_empty());
}
