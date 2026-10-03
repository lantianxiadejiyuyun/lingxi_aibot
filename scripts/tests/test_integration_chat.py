"""Account isolation and stable REST/SSE chat contracts, without provider calls."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from flask import g
from flask_login import current_user

from scripts.tests.support import IsolatedAppTestCase, make_app
from app.extensions import db
from app.models.conversation import Conversation, Message
from app.models.setting import Setting
from app.models.task import Task
from app.models.user import User
from app.services.integration_chat_service import get_conversation, iter_chat_events, validate_chat_payload, validate_title
from app.services.settings_service import set_setting
from app.utils.scoping import current_user_id, set_current_user_id, user_scope


class IntegrationChatTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user("integration_chat_user", is_admin=False)
        self.other = self.make_user("integration_chat_other")
        self.user.api_token = "lx_test_chat_user_secret"
        self.other.api_token = "lx_test_chat_other_secret"
        self.conversation = Conversation(user_id=self.user.id, title="我的会话")
        self.foreign = Conversation(user_id=self.other.id, title="私密会话")
        db.session.add_all([self.conversation, self.foreign])
        db.session.commit()
        self.client = self.app.test_client()
        self.headers = {"Authorization": "Bearer " + self.user.api_token}

    def request(self, method, path, **kwargs):
        return self.client.open("/api/v1" + path, method=method, headers=self.headers, **kwargs)

    def test_token_required_and_cookie_cannot_authorize(self):
        self.assertEqual(self.client.get("/api/v1/conversations").status_code, 401)
        cookie_client = self.client_for(self.user)
        self.assertEqual(cookie_client.get("/api/v1/conversations").status_code, 401)

    def test_conversations_crud_and_metadata_exclude_configuration(self):
        response = self.request("POST", "/conversations", json={"title": "外部客户端"})
        self.assertEqual(response.status_code, 201, response.get_json())
        created = response.get_json()["data"]
        self.assertEqual(set(created), {"id", "title", "created_at", "updated_at"})
        self.assertTrue(created["created_at"].endswith("Z"))
        identifier = created["id"]
        response = self.request("PATCH", f"/conversations/{identifier}", json={"title": "新标题"})
        self.assertEqual(response.get_json()["data"]["title"], "新标题")
        listed = self.request("GET", "/conversations?limit=1&offset=0").get_json()
        self.assertEqual(listed["pagination"]["total"], 2)
        self.assertEqual(len(listed["data"]), 1)
        self.assertNotIn("私密会话", json.dumps(listed, ensure_ascii=False))
        self.assertEqual(self.request("GET", f"/conversations/{identifier}").status_code, 200)
        db.session.add(Message(conversation_id=identifier, role="user", content="delete me"))
        db.session.add(Setting(user_id=self.user.id, key=f"chat_llm:{identifier}", value={"model": "private"}))
        db.session.commit()
        self.assertEqual(self.request("DELETE", f"/conversations/{identifier}").status_code, 200)
        self.assertIsNone(db.session.get(Conversation, identifier))
        self.assertEqual(Message.query.filter_by(conversation_id=identifier).count(), 0)
        self.assertEqual(Setting.query.filter_by(key=f"chat_llm:{identifier}").count(), 0)

    def test_foreign_id_never_falls_back_to_new_conversation(self):
        identifier = self.foreign.id
        count = Conversation.query.count()
        for method, path, body in (
                ("GET", f"/conversations/{identifier}", None),
                ("GET", f"/conversations/{identifier}/messages", None),
                ("PATCH", f"/conversations/{identifier}", {"title": "attack"}),
                ("DELETE", f"/conversations/{identifier}", None),
                ("POST", f"/conversations/{identifier}/messages", {"message": "/help"}),
                ("POST", "/chat", {"message": "/help", "conversation_id": identifier})):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path, json=body).status_code, 404)
        self.assertEqual(Conversation.query.count(), count)
        self.assertEqual(Message.query.count(), 0)
        self.assertEqual(db.session.get(Conversation, identifier).title, "私密会话")

    def test_history_is_owned_and_paginated_in_creation_order(self):
        db.session.add_all([
            Message(conversation_id=self.conversation.id, role="user", content="first"),
            Message(conversation_id=self.conversation.id, role="tool", content='{"id":7}'),
            Message(conversation_id=self.conversation.id, role="assistant", content="last"),
            Message(conversation_id=self.foreign.id, role="user", content="foreign secret"),
        ])
        db.session.commit()
        response = self.request("GET", f"/conversations/{self.conversation.id}/messages?limit=1&offset=1")
        body = response.get_json()
        self.assertEqual(body["pagination"]["total"], 3)
        self.assertEqual(body["data"][0]["role"], "tool")
        self.assertNotIn("foreign secret", response.get_data(as_text=True))
        self.assertEqual(self.request("GET", "/conversations?limit=201").status_code, 400)

    def test_help_without_model_key_works_and_persists_under_token_owner(self):
        # A different web login must not override the Bearer token's identity.
        self.client = self.client_for(self.other)
        response = self.request("POST", "/chat", json={"message": "/help", "request_id": "help-1"})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertFalse(any("session=" in header for header in response.headers.getlist("Set-Cookie")))
        body = response.get_json()["data"]
        self.assertIn("/model", body["reply"])
        self.assertEqual(db.session.get(Conversation, body["conversation_id"]).user_id, self.user.id)
        self.assertEqual(Message.query.filter_by(conversation_id=body["conversation_id"]).count(), 2)
        events = body["events"]
        self.assertEqual(events[0]["type"], "start")
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["data"], body["reply"])
        self.assertEqual([event["seq"] for event in events], list(range(1, len(events) + 1)))
        self.assertTrue(all(event["request_id"] == "help-1" for event in events))

    def test_sse_event_name_and_json_payload_have_same_contract(self):
        response = self.request("POST", f"/conversations/{self.conversation.id}/messages",
                                json={"message": "/help", "stream": True, "request_id": "stream-1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/event-stream")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Accel-Buffering"], "no")
        events = []
        for block in response.get_data(as_text=True).strip().split("\n\n"):
            lines = block.splitlines()
            event = json.loads(lines[1][len("data: "):])
            self.assertEqual(lines[0], "event: " + event["type"])
            self.assertEqual(set(event), {"type", "conversation_id", "request_id", "seq", "data"})
            self.assertEqual(event["conversation_id"], self.conversation.id)
            events.append(event)
        self.assertEqual(events[0]["data"], {})
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(sum(event["type"] == "done" for event in events), 1)

    def test_strict_json_and_message_validation_precede_creation(self):
        count = Conversation.query.count()
        bodies = ([], None, {}, {"message": " "}, {"message": 12},
                  {"message": "x" * 100001}, {"message": "/help", "stream": "true"},
                  {"message": "/help", "conversation_id": True},
                  {"message": "/help", "conversation_id": "1"},
                  {"message": "/help", "request_id": "x" * 129})
        for body in bodies:
            with self.subTest(body=repr(body)[:70]):
                self.assertEqual(self.request("POST", "/chat", json=body).status_code, 400)
        self.assertEqual(self.request("POST", "/chat", data="{broken", content_type="application/json").status_code, 400)
        self.assertEqual(Conversation.query.count(), count)
        self.assertEqual(Message.query.count(), 0)

    def test_path_and_body_id_disagreement_is_rejected(self):
        response = self.request("POST", f"/conversations/{self.conversation.id}/messages",
                                json={"message": "/help", "conversation_id": self.foreign.id})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Message.query.count(), 0)

    def test_invalid_titles_do_not_mutate_conversation(self):
        for title in (None, "", [], "x" * 256):
            self.assertEqual(self.request("POST", "/conversations", json={"title": title}).status_code, 400)
            self.assertEqual(self.request("PATCH", f"/conversations/{self.conversation.id}",
                                          json={"title": title}).status_code, 400)
        self.assertEqual(self.request("PATCH", f"/conversations/{self.conversation.id}",
                                      json={"user_id": self.other.id}).status_code, 400)
        self.assertEqual(db.session.get(Conversation, self.conversation.id).title, "我的会话")

    def test_error_is_not_success_and_always_has_terminal_done(self):
        response = self.request("POST", "/chat", json={"message": "hello"})
        self.assertEqual(response.status_code, 502)
        body = response.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["code"], "chat_failed")
        self.assertIn("未配置", body["error"])
        self.assertEqual([event["type"] for event in body["data"]["events"]], ["start", "error", "done"])
        self.assertEqual(body["data"]["reply"], "")

    def test_unexpected_and_legacy_executor_errors_do_not_expose_secrets(self):
        for mocked in (patch("app.services.integration_chat_service.run_chat", side_effect=RuntimeError("sk-private-url")),
                       patch("app.services.integration_chat_service.run_chat", return_value=iter([("error", "sk-private-url")]))):
            with mocked:
                response = self.request("POST", "/chat", json={"message": "hello"})
            self.assertEqual(response.status_code, 502)
            self.assertNotIn("sk-private-url", response.get_data(as_text=True))
            self.assertEqual(response.get_json()["data"]["events"][-1]["type"], "done")

    def test_generator_close_restores_existing_user_scope_and_closes_executor(self):
        closed = []

        def execute(conversation, message, user):
            try:
                self.assertEqual(current_user.id, self.user.id)
                self.assertEqual(current_user_id(), self.user.id)
                yield "delta", "partial"
                yield "done", "partial"
            finally:
                closed.append(True)

        set_current_user_id(self.other.id)
        with self.app.test_request_context("/"), patch("app.services.integration_chat_service.run_chat", execute):
            g._login_user = self.other
            events = iter_chat_events(self.conversation.id, self.user.id, "hello")
            self.assertEqual(next(events)["type"], "start")
            self.assertEqual(next(events)["type"], "delta")
            events.close()
            self.assertEqual(current_user.id, self.other.id)
            self.assertEqual(current_user_id(), self.other.id)
        self.assertEqual(closed, [True])

    def test_real_tool_execution_uses_token_account_settings_and_user(self):
        from app.ai.llm import LLMClient

        for user, model in ((self.user, "integration-model"), (self.other, "foreign-model")):
            with user_scope(user.id):
                set_setting("llm_api_key", "isolated-not-real-key")
                set_setting("llm_base_url", "https://model.invalid/v1")
                set_setting("llm_model", model)
                set_setting("ai_persona_ack_enabled", False)
        calls = []

        def stream(client, messages, tools=None):
            self.assertEqual(current_user.id, self.user.id)
            self.assertEqual(current_user_id(), self.user.id)
            self.assertEqual(client._read_config()["model"], "integration-model")
            calls.append(True)
            if len(calls) == 1:
                yield {"type": "tool_calls", "calls": [{"id": "call_integration_task", "name": "create_task",
                                                        "arguments": json.dumps({"title": "API 创建的任务"})}]}
            else:
                yield {"type": "delta", "text": "已经创建"}

        self.client = self.client_for(self.other)
        with patch.object(LLMClient, "chat_stream", stream):
            response = self.request("POST", "/chat", json={"message": "帮我创建任务"})
        self.assertEqual(response.status_code, 200, response.get_json())
        task = Task.query.one()
        self.assertEqual(task.user_id, self.user.id)
        self.assertEqual(task.title, "API 创建的任务")
        tool_events = [event for event in response.get_json()["data"]["events"] if event["type"] == "tool"]
        self.assertEqual(tool_events[0]["data"]["name"], "create_task")
        self.assertTrue(tool_events[0]["data"]["ok"])
        self.assertEqual(len(calls), 2)

    def test_payload_validator_supports_websocket_and_requires_requested_conversation(self):
        from app.utils.integration_api import ApiError

        payload = validate_chat_payload({"message": "  hello  ", "conversation_id": self.conversation.id}, True)
        self.assertEqual(payload["message"], "hello")
        self.assertFalse(payload["stream"])
        with self.assertRaises(ApiError):
            validate_chat_payload({"message": "hello"}, True)

    def test_shared_service_rejects_invalid_unicode_and_oversized_ids(self):
        from app.utils.integration_api import ApiError

        for field in ("message", "request_id"):
            with self.subTest(field=field), self.assertRaises(ApiError):
                validate_chat_payload({"message": "hello", field: "bad\ud800"})
        with self.assertRaises(ApiError):
            validate_title("bad\ud800")
        for identifier in (2147483648, 10**100):
            with self.subTest(identifier=identifier), self.assertRaises(ApiError):
                validate_chat_payload({"message": "hello", "conversation_id": identifier})
            self.assertEqual(self.request("GET", f"/conversations/{identifier}").status_code, 400)


class IntegrationChatConcurrencyTests(unittest.TestCase):
    """File SQLite gives each request thread an independent database session."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lingxi-chat-lock-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.app = make_app(SQLALCHEMY_DATABASE_URI="sqlite:///" + str(root / "chat.sqlite").replace("\\", "/"),
                            DATA_DIR=root, IMAGE_DIR=root / "images", BACKUP_DIR=root / "backups")
        self.addCleanup(self._close_database)
        with self.app.app_context():
            db.create_all()
            user = User(username="chat_lock_user", api_token="lx_isolated_chat_lock_token")
            user.set_password("isolated-test-password")
            db.session.add(user)
            db.session.flush()
            conversation = Conversation(user_id=user.id, title="before")
            db.session.add(conversation)
            db.session.commit()
            self.user_id, self.conversation_id = user.id, conversation.id
            self.headers = {"Authorization": "Bearer " + user.api_token}
        for target in ("requests.sessions.Session.request", "httpx.Client.send"):
            blocker = patch(target, side_effect=AssertionError("External HTTP disabled in concurrency tests"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def _close_database(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def _assert_web_deletion_waits_and_preserves_other_account(self, clear_all):
        self.app.config["WTF_CSRF_ENABLED"] = False
        with self.app.app_context():
            other = User(username="foreign_chat_owner")
            other.set_password("isolated-other-password")
            db.session.add(other)
            db.session.flush()
            foreign = Conversation(user_id=other.id, title="foreign stays")
            extra = Conversation(user_id=self.user_id, title="another own conversation")
            db.session.add_all([foreign, extra])
            db.session.flush()
            foreign_id, foreign_user_id, extra_id = foreign.id, other.id, extra.id
            db.session.add_all([
                Setting(user_id=other.id, key=f"chat_llm:{foreign.id}", value={"model": "foreign-model"}),
                Setting(user_id=other.id, key=f"chat_context:{foreign.id}", value={"through_id": 1}),
                Setting(user_id=self.user_id, key=f"chat_context:{self.conversation_id}", value={"through_id": 0}),
                Setting(user_id=self.user_id, key=f"chat_llm:{extra.id}", value={"model": "own-model"}),
                Setting(user_id=self.user_id, key="llm_model", value="keep-account-setting"),
                Message(conversation_id=foreign.id, role="user", content="foreign message"),
                Message(conversation_id=extra.id, role="user", content="own extra message"),
            ])
            db.session.commit()
        web_client = self.app.test_client()
        with web_client.session_transaction() as session:
            session["_user_id"] = str(self.user_id)
            session["_fresh"] = True
        attempted, finished = threading.Event(), threading.Event()
        responses = []

        def delete_web():
            try:
                attempted.set()
                path = "/chat/api/clear-all" if clear_all else f"/chat/api/delete/{self.conversation_id}"
                response = web_client.post(path)
                responses.append((response.status_code, response.get_json()))
            finally:
                finished.set()

        worker = threading.Thread(target=delete_web, daemon=True)
        with self.app.test_request_context("/"):
            iterator = iter_chat_events(self.conversation_id, self.user_id, "/auto on")
            try:
                self.assertEqual(next(iterator)["type"], "start")
                worker.start()
                self.assertTrue(attempted.wait(2))
                self.assertFalse(finished.wait(0.15), "Web deletion overtook an active API chat")
                events = list(iterator)
                self.assertEqual(events[-1]["type"], "done")
                self.assertFalse(any(event["type"] == "error" for event in events))
            finally:
                iterator.close()
                if worker.ident is not None:
                    worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "Web deletion deadlocked while acquiring conversation locks")
        self.assertEqual(responses[0][0], 200, responses)
        self.assertTrue(responses[0][1]["ok"])
        if clear_all:
            self.assertEqual(responses[0][1]["data"]["deleted"], 2)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Conversation, self.conversation_id))
            self.assertEqual(Message.query.filter_by(conversation_id=self.conversation_id).count(), 0)
            self.assertEqual(Setting.query.filter(Setting.user_id == self.user_id, Setting.key.in_(
                [f"chat_llm:{self.conversation_id}", f"chat_context:{self.conversation_id}"])).count(), 0)
            self.assertEqual(db.session.get(Conversation, foreign_id).title, "foreign stays")
            self.assertEqual(Message.query.filter_by(conversation_id=foreign_id).one().content, "foreign message")
            self.assertEqual(Setting.query.filter_by(user_id=foreign_user_id).count(), 2)
            self.assertEqual(Setting.query.filter_by(user_id=self.user_id, key="llm_model").one().value,
                             "keep-account-setting")
            self.assertEqual(Conversation.query.filter_by(id=extra_id).count(), 0 if clear_all else 1)
            self.assertEqual(Setting.query.filter_by(user_id=self.user_id, key=f"chat_llm:{extra_id}").count(),
                             0 if clear_all else 1)

    def test_web_delete_waits_for_api_chat_and_preserves_other_account(self):
        self._assert_web_deletion_waits_and_preserves_other_account(clear_all=False)

    def test_web_clear_all_waits_without_deadlock_and_preserves_other_account(self):
        self._assert_web_deletion_waits_and_preserves_other_account(clear_all=True)

    def test_delete_waits_from_start_until_generator_closed_without_orphan_settings(self):
        attempted = threading.Event()
        deleted = threading.Event()
        result = []

        def delete():
            try:
                attempted.set()
                response = self.app.test_client().delete(f"/api/v1/conversations/{self.conversation_id}",
                                                         headers=self.headers)
                result.append(response.status_code)
            finally:
                deleted.set()

        worker = threading.Thread(target=delete)
        with self.app.test_request_context("/"):
            iterator = iter_chat_events(self.conversation_id, self.user_id, "/auto on")
            try:
                self.assertEqual(next(iterator)["type"], "start")
                worker.start()
                self.assertTrue(attempted.wait(2))
                self.assertFalse(deleted.wait(0.15), "DELETE overtook the active stream's start event")
                events = []
                for _ in range(10):
                    event = next(iterator)
                    events.append(event)
                    if event["type"] == "done":
                        break
                self.assertEqual(events[-1]["type"], "done")
                self.assertFalse(any(event["type"] == "error" for event in events))
                self.assertEqual(Setting.query.filter_by(key=f"chat_llm:{self.conversation_id}").count(), 1)
                self.assertFalse(deleted.is_set(), "DELETE must wait until the terminal event is consumed")
            finally:
                iterator.close()
                if worker.ident is not None:
                    worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [200])
        with self.app.app_context():
            self.assertIsNone(db.session.get(Conversation, self.conversation_id))
            self.assertEqual(Setting.query.filter_by(key=f"chat_llm:{self.conversation_id}").count(), 0)
            self.assertEqual(Message.query.filter_by(conversation_id=self.conversation_id).count(), 0)

    def test_deletion_after_rest_prevalidation_returns_sse_error_and_done(self):
        real_iterator = iter_chat_events

        def delete_before_iteration(*args, **kwargs):
            # A separate context/session emulates DELETE winning the queue after
            # the REST route checked ownership, before response iteration began.
            with self.app.app_context():
                Conversation.query.filter_by(id=self.conversation_id).delete()
                db.session.commit()
            yield from real_iterator(*args, **kwargs)

        with patch("app.blueprints.integration_chat.iter_chat_events", delete_before_iteration):
            response = self.app.test_client().post("/api/v1/chat", headers=self.headers,
                                                   json={"conversation_id": self.conversation_id,
                                                         "message": "/auto on", "stream": True})
            self.assertEqual(response.status_code, 200)
            payload = response.get_data(as_text=True)
        events = [json.loads(line[len("data: "):]) for line in payload.splitlines()
                  if line.startswith("data: ")]
        self.assertEqual([event["type"] for event in events], ["start", "error", "done"])
        self.assertEqual(events[1]["data"], "会话不存在")
        self.assertEqual(events[-1]["data"], "")
        self.assertNotIn("<!doctype", payload.lower())
        with self.app.app_context():
            self.assertEqual(Setting.query.count(), 0)
            self.assertEqual(Message.query.count(), 0)

    def test_lookup_and_execution_refresh_preloaded_identity(self):
        observed = []

        def execute(conversation, message, user):
            observed.append(conversation.title)
            yield "done", "okay"

        with self.app.test_request_context("/"):
            stale = db.session.get(Conversation, self.conversation_id)
            self.assertEqual(stale.title, "before")
            with self.app.app_context():
                Conversation.query.filter_by(id=self.conversation_id).update({"title": "after"})
                db.session.commit()
            # populate_existing refreshes cached ORM fields on a new query.
            self.assertEqual(get_conversation(self.user_id, self.conversation_id).title, "after")
            with self.app.app_context():
                Conversation.query.filter_by(id=self.conversation_id).update({"title": "latest"})
                db.session.commit()
            with patch("app.services.integration_chat_service.run_chat", execute):
                events = list(iter_chat_events(self.conversation_id, self.user_id, "hello"))
        self.assertEqual(observed, ["latest"])
        self.assertEqual(events[-1]["data"], "okay")
