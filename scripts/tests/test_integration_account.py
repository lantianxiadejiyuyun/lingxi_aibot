from datetime import datetime
from unittest.mock import patch, PropertyMock

from flask import g
from flask_login import current_user

from scripts.tests.support import IsolatedAppTestCase
from app.extensions import db
from app.utils.api_auth import assign_user_api_token, revoke_user_api_token
from app.utils.integration_api import ApiError, api_user_context, parse_datetime
from app.utils.scoping import current_user_id, user_scope


class IntegrationAccountTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user("token_owner", is_admin=False)
        self.admin = self.make_user("browser_admin")
        self.token = assign_user_api_token(self.user)
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.client = self.app.test_client()

    def test_token_identity_overrides_cookie_without_logging_in(self):
        client = self.client_for(self.admin)
        response = client.get("/api/v1/me", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        data = response.json["data"]
        self.assertEqual(data["id"], self.user.id)
        self.assertEqual(data["username"], "token_owner")
        self.assertEqual(data["websocket_path"], "/api/v1/ws")
        self.assertNotIn(self.token, response.get_data(as_text=True))
        self.assertNotIn("Set-Cookie", response.headers)
        with client.session_transaction() as session:
            self.assertEqual(session["_user_id"], str(self.admin.id))

    def test_cookie_alone_and_url_token_do_not_authenticate(self):
        client = self.client_for(self.admin)
        for path in ("/api/v1/me", f"/api/v1/me?token={self.token}"):
            response = client.get(path)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json["code"], "unauthorized")
            self.assertIn("WWW-Authenticate", response.headers)

    def test_rotation_and_revocation_take_effect(self):
        replacement = assign_user_api_token(self.user)
        self.assertEqual(self.client.get("/api/v1/me", headers=self.headers).status_code, 401)
        headers = {"X-API-Token": replacement}
        self.assertEqual(self.client.get("/api/v1/me", headers=headers).status_code, 200)
        revoke_user_api_token(self.user)
        self.assertEqual(self.client.get("/api/v1/me", headers=headers).status_code, 401)

    def test_legacy_environment_token_and_minimum_length(self):
        self.app.config["API_TOKEN"] = "legacy-test-token-at-least-16"
        response = self.client.get("/api/v1/me", headers={"X-API-Token": self.app.config["API_TOKEN"]})
        self.assertEqual(response.json["data"]["id"], self.admin.id)
        self.app.config["API_TOKEN"] = "short"
        self.assertEqual(self.client.get("/api/v1/me", headers={"X-API-Token": "short"}).status_code, 401)

    def test_scope_restored_even_when_tool_raises(self):
        with self.app.test_request_context("/api/v1/me"), user_scope(self.admin.id):
            g._login_user = self.admin
            with self.assertRaises(RuntimeError):
                with api_user_context(self.user):
                    self.assertEqual(current_user.id, self.user.id)
                    self.assertEqual(current_user_id(), self.user.id)
                    raise RuntimeError("test cleanup")
            self.assertEqual(current_user.id, self.admin.id)
            self.assertEqual(current_user_id(), self.admin.id)
            self.assertNotIn("api_user", g)

    def test_prefix_errors_and_cache_contract(self):
        self.app.config["ADMIN_ENTRY"] = "lingxi"
        response = self.client.get("/lingxi/api/v1/me", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["data"]["websocket_path"], "/lingxi/api/v1/ws")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.get("/api/v1/me", headers=self.headers).status_code, 404)
        for method, path, status in (("get", "/lingxi/api/v1/missing", 404),
                                     ("post", "/lingxi/api/v1/me", 405)):
            result = getattr(self.client, method)(path, headers=self.headers)
            self.assertEqual(result.status_code, status)
            self.assertFalse(result.json["ok"])

    def test_datetime_explicit_zone_and_null_contract(self):
        self.assertEqual(parse_datetime("2026-10-03T08:00:00+08:00", "start"), datetime(2026, 10, 3))
        self.assertIsNone(parse_datetime(None, "end", nullable=True))
        for value in ("2026-10-03", "2026-10-03T00:00:00", "2026-10-03T00:00:00+0800",
                      "2026-10-03T00:00:00+08:99", "2026-10-03T00:00:00+24:00",
                      "2026-02-30T00:00:00Z", True, [], None):
            with self.subTest(value=value), self.assertRaises(ApiError):
                parse_datetime(value, "start")

    def test_json_request_limit_and_invalid_body(self):
        response = self.client.post("/api/v1/conversations", json=[], headers=self.headers)
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/v1/conversations", data='{"title":"' + 'x' * (1024 * 1024) + '"}',
                                    content_type="application/json", headers=self.headers)
        self.assertEqual(response.status_code, 413)
        self.assertFalse(response.json["ok"])
        for payload in ('{"title":"bad\\ud800"}', '{"title":NaN}', '{"title":' + '[' * 1100 + '0' + ']' * 1100 + '}'):
            response = self.client.post("/api/v1/conversations", data=payload, content_type="application/json", headers=self.headers)
            self.assertEqual(response.status_code, 400)
            self.assertFalse(response.json["ok"])

    def test_upstream_exception_is_not_persisted_to_api_history(self):
        secret = "test-upstream-secret-that-must-not-be-saved"
        with patch("app.ai.executor.LLMClient.is_configured", new_callable=PropertyMock, return_value=True), \
                patch("app.ai.executor._build_runtime_messages", return_value=[]), \
                patch("app.services.context_service.maybe_compact_conversation", return_value={}), \
                patch("app.ai.executor.LLMClient.chat_stream", side_effect=RuntimeError(secret)), \
                self.assertLogs("app.ai.executor", level="ERROR") as logs:
            response = self.client.post("/api/v1/chat", json={"message": "test safe failure"}, headers=self.headers)
        self.assertEqual(response.status_code, 502)
        conversation_id = response.json["data"]["conversation_id"]
        history = self.client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=self.headers)
        self.assertNotIn(secret, response.get_data(as_text=True))
        self.assertNotIn(secret, history.get_data(as_text=True))
        self.assertNotIn(secret, " ".join(logs.output))
        self.assertTrue(any("处理中断" in row["content"] for row in history.json["data"]))
