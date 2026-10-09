"""Media HTTP/WS contracts against an isolated real database and filesystem."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from itsdangerous import URLSafeTimedSerializer

from scripts.tests.support import IsolatedAppTestCase
from scripts.tests.test_integration_ws import FakeSocket
from app.extensions import db
from app.models.agent_run import AgentRun
from app.models.media import DownloadTask, MediaEvent
from app.models.setting import Setting
from app.models.user import User
from app.services import media_ws, native_agent_service, storage_service
from app.services.integration_ws import _ConnectionSlots
from app.utils.api_auth import assign_user_api_token


def packet(**data):
    return json.dumps(data)


class MediaApiTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.other = self.make_user("other_media_admin")
        self.user_id, self.other_id = self.user.id, self.other.id
        self.token = assign_user_api_token(self.user)
        self.other_token = assign_user_api_token(self.other)
        self.client = self.app.test_client(use_cookies=False)
        self.disk = Path(self.temp_directory.name) / "owned-disk"
        self.disk.mkdir()
        (self.disk / "ready.txt").write_text("Owned file content", encoding="utf-8")
        location = storage_service.create_location(self.user_id, {
            "name": "Owned storage", "root_path": str(self.disk), "min_free_bytes": 0,
        })
        self.storage_id = location.id

    def request(self, method, path, body=None, *, other=False, **kwargs):
        headers = {"Authorization": "Bearer " + (self.other_token if other else self.token)}
        headers.update(kwargs.pop("headers", {}))
        return self.client.open("/api/v1" + path, method=method, json=body, headers=headers, **kwargs)

    def create_download(self):
        response = self.request("POST", "/downloads", {
            "title": "Synthetic series", "episode": "1", "storage_id": self.storage_id,
            "request_key": "same-user-request",
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["data"]

    def serve(self, frames, query=""):
        slots = _ConnectionSlots()
        sock = FakeSocket(frames)
        with self.app.test_request_context("/api/v1/download-events/ws" + query):
            media_ws.serve_media_socket(sock, slots)
        self.assertEqual(slots.total, 0)
        self.assertEqual(dict(slots.users), {})
        return sock

    def test_http_requires_token_and_does_not_set_login_cookie(self):
        for path in ("/downloads", "/storage-locations", "/agent-runs", "/download-events"):
            response = self.client.get("/api/v1" + path)
            self.assertEqual(response.status_code, 401, path)
            self.assertEqual(response.get_json()["code"], "unauthorized")
        response = self.request("GET", "/storage-locations")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get("Set-Cookie"))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        browser = self.client_for(self.user)
        self.assertEqual(browser.get("/api/v1/downloads").status_code, 401)

    def test_browser_mutation_requires_csrf_but_token_api_does_not(self):
        browser = self.client_for(self.user)
        payload = {"prompt": "Prepare an offline research task", "allowed_tools": []}
        missing = browser.post("/media/api/agent-runs", json=payload)
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(AgentRun.query.count(), 0)
        with browser.session_transaction() as session:
            session["csrf_token"] = "media-csrf-seed"
        csrf_token = URLSafeTimedSerializer(self.app.secret_key, salt="wtf-csrf-token").dumps("media-csrf-seed")
        valid = browser.post("/media/api/agent-runs", json=payload, headers={"X-CSRFToken": csrf_token})
        self.assertEqual(valid.status_code, 201, valid.get_data(as_text=True))
        token_response = self.request("POST", "/agent-runs", payload)
        self.assertEqual(token_response.status_code, 201)

    def test_admin_accounts_cannot_read_each_others_storage_files_or_tasks(self):
        task = self.create_download()
        self.assertEqual(self.request("GET", "/storage-locations", other=True).get_json()["data"], [])
        self.assertEqual(self.request("GET", "/downloads", other=True).get_json()["data"], [])
        paths = [f"/files?storage_id={self.storage_id}",
                 f"/files/download?storage_id={self.storage_id}&path=ready.txt",
                 f"/downloads/{task['id']}", f"/downloads/{task['id']}/events",
                 f"/downloads/{task['id']}/stream"]
        for path in paths:
            with self.subTest(path=path):
                denied = self.request("GET", path, other=True)
                self.assertEqual(denied.status_code, 404)
                self.assertNotIn("Owned", denied.get_data(as_text=True))
        denied_create = self.request("POST", "/downloads", {"title": "Other", "storage_id": self.storage_id}, other=True)
        self.assertEqual(denied_create.status_code, 404)
        self.assertEqual(DownloadTask.query.count(), 1)

    def test_owned_file_download_and_path_traversal_rejection(self):
        listing = self.request("GET", f"/files?storage_id={self.storage_id}").get_json()["data"]
        self.assertEqual([row["name"] for row in listing["entries"]], ["ready.txt"])
        response = self.request("GET", f"/files/download?storage_id={self.storage_id}&path=ready.txt")
        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"Owned file content")
        finally:
            response.close()
        for path in ("../outside", "/outside", ".lingxi-pending", "C:/Windows"):
            denied = self.request("GET", "/files", query_string={"storage_id": self.storage_id, "path": path})
            self.assertIn(denied.status_code, (400, 404))

    def test_configuration_credentials_are_encrypted_and_never_echoed(self):
        payload = {"downloaders": [
            {"kind": "qbittorrent", "base_url": "http://192.168.1.10:8080", "username": "private-user", "password": "private-password"},
            {"kind": "aria2", "base_url": "http://192.168.1.10:6800", "secret": "private-rpc-secret"},
        ], "sources": [{"kind": "rss", "name": "My source", "url": "https://example.com/rss?api_key=private-feed-key"}]}
        with patch("app.services.media_sources.validate_public_url", side_effect=lambda url: url):
            saved = self.request("PUT", "/media/config", payload)
        self.assertEqual(saved.status_code, 200, saved.get_json())
        loaded = self.request("GET", "/media/config")
        stored = Setting.query.filter_by(user_id=self.user_id, key="media_config").one()
        for secret in ("private-user", "private-password", "private-rpc-secret", "private-feed-key"):
            self.assertNotIn(secret, saved.get_data(as_text=True))
            self.assertNotIn(secret, loaded.get_data(as_text=True))
            self.assertNotIn(secret, json.dumps(stored.value))
        self.assertEqual(self.request("GET", "/media/config", other=True).get_json()["data"]["downloaders"], [])
        exposed = [{"title": "Example", "kind": "http", "fingerprint": "abc",
                    "url": "https://example.com/video?token=private-file-token",
                    "source_url": "https://example.com/rss?api_key=private-feed-key"}]
        with patch("app.services.media_sources.search_resources", return_value=exposed):
            response = self.request("POST", "/anime/search", {"title": "Example"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("private-", response.get_data(as_text=True))

    def test_nonadmin_cannot_register_server_paths_or_downloaders(self):
        member = self.make_user("media_member", False)
        token = assign_user_api_token(member)
        headers = {"Authorization": "Bearer " + token}
        for path, method, body in (("storage-locations", "POST", {"name": "server disk", "root_path": str(self.disk)}),
                                   ("media/config", "PUT", {"downloaders": []})):
            response = self.client.open("/api/v1/" + path, method=method, json=body, headers=headers)
            self.assertEqual(response.status_code, 403)

    def test_download_creation_replay_events_and_control(self):
        task = self.create_download()
        repeated = self.create_download()
        self.assertEqual(repeated["id"], task["id"])
        self.assertEqual(DownloadTask.query.count(), 1)
        initial = self.request("GET", f"/downloads/{task['id']}/events").get_json()["data"]
        self.assertEqual([row["type"] for row in initial], ["created"])
        last_seq = initial[-1]["seq"]
        action = self.request("POST", f"/downloads/{task['id']}/actions", {"action": "cancel"})
        self.assertEqual(action.status_code, 200)
        self.assertEqual(action.get_json()["data"]["control"], "cancel")
        replay = self.request("GET", f"/downloads/{task['id']}/events?after={last_seq}").get_json()["data"]
        self.assertEqual([row["type"] for row in replay], ["control_requested"])
        self.assertTrue(all(row["seq"] > last_seq for row in replay))
        foreign = self.request("POST", f"/downloads/{task['id']}/actions", {"action": "pause"}, other=True)
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(db.session.get(DownloadTask, task["id"]).control, "cancel")

    def test_general_agent_api_queue_details_cancel_and_tenant_isolation(self):
        with patch.object(native_agent_service, "LLMClient") as llm:
            response = self.request("POST", "/agent-runs", {"prompt": "研究任务", "allowed_tools": [], "request_key": "api-test"})
            self.assertEqual(response.status_code, 201, response.get_json())
            llm.assert_not_called()
        item = response.get_json()["data"]
        self.assertEqual(item["status"], "queued")
        self.assertEqual(self.request("GET", "/agent-runs").get_json()["data"][0]["run_id"], item["run_id"])
        detail = self.request("GET", f"/agent-runs/{item['run_id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn("checkpoint", detail.get_json()["data"])
        self.assertEqual(self.request("GET", "/agent-runs", other=True).get_json()["data"], [])
        for method, path in (("GET", f"/agent-runs/{item['run_id']}"), ("POST", f"/agent-runs/{item['run_id']}/cancel")):
            denied = self.request(method, path, {}, other=True)
            self.assertIn(denied.status_code, (400, 404))
            self.assertNotIn("研究任务", denied.get_data(as_text=True))
        cancelled = self.request("POST", f"/agent-runs/{item['run_id']}/cancel", {})
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.get_json()["data"]["status"], "cancelled")
        bad = self.request("POST", "/agent-runs", {"prompt": "Invalid tool", "allowed_tools": ["download_anime"]})
        self.assertEqual(bad.status_code, 400)

    def test_sse_honors_last_event_id_and_releases_stream(self):
        task = self.create_download()
        initial = self.request("GET", f"/downloads/{task['id']}/events").get_json()["data"]
        self.request("POST", f"/downloads/{task['id']}/actions", {"action": "pause"})
        response = self.request("GET", f"/downloads/{task['id']}/stream",
                                headers={"Last-Event-ID": str(initial[-1]["seq"])}, buffered=False)
        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "text/event-stream")
            frame = next(iter(response.response)).decode("utf-8")
            self.assertIn('"type": "control_requested"', frame)
            self.assertNotIn('"type": "created"', frame)
            self.assertIn("event: media", frame)
        finally:
            response.close()

    def test_ws_reconnect_replays_after_sequence_and_handles_cancel(self):
        task = self.create_download()
        first = self.serve([packet(type="auth", token=self.token), packet(type="subscribe", task_id=task["id"], after=0)])
        events = [item for item in first.sent if "seq" in item and item.get("task_id") == task["id"]]
        self.assertEqual([item["type"] for item in events], ["created"])
        last_seq = events[-1]["seq"]
        second = self.serve([packet(type="auth", token=self.token),
                             packet(type="subscribe", task_id=task["id"], after=last_seq),
                             packet(type="download.action", task_id=task["id"], action="cancel"),
                             packet(type="ping")])
        self.assertIn("ack", [item["type"] for item in second.sent])
        self.assertIn("pong", [item["type"] for item in second.sent])
        replayed = [item for item in second.sent if "seq" in item and item.get("task_id") == task["id"]]
        self.assertEqual([item["type"] for item in replayed], ["control_requested"])
        self.assertTrue(all(item["seq"] > last_seq for item in replayed))

    def test_ws_foreign_subscription_and_commands_do_not_leak_or_stop_valid_socket(self):
        task = self.create_download()
        foreign_event = MediaEvent(user_id=self.other_id, task_id=999, kind="private-event", data={"secret": "foreign-evidence"})
        db.session.add(foreign_event)
        db.session.commit()
        sock = self.serve([packet(type="auth", token=self.other_token),
                           packet(type="subscribe", after=0),
                           packet(type="subscribe", task_id=task["id"], after=0),
                           packet(type="download.action", task_id=task["id"], action="cancel"),
                           packet(type="ping")])
        self.assertEqual(sock.sent[-1]["type"], "pong")
        self.assertEqual(sum(item["type"] == "error" for item in sock.sent), 2)
        self.assertNotIn("Synthetic series", json.dumps(sock.sent))
        self.assertEqual(db.session.get(DownloadTask, task["id"]).control, "")
        owned = self.serve([packet(type="auth", token=self.token), packet(type="subscribe", after=0)])
        self.assertNotIn("foreign-evidence", json.dumps(owned.sent))

    def test_ws_rejects_bad_auth_and_query_credentials(self):
        for first in (None, "[]", "not json", packet(type="auth", token="invalid")):
            with self.subTest(first=first):
                sock = self.serve([first])
                self.assertIsNotNone(sock.closed)
                self.assertEqual(sock.closed[0], 1008)
        sock = self.serve([packet(type="auth", token=self.token)], query="?token=secret")
        self.assertEqual(sock.closed[0], 1008)
        self.assertEqual(sock.timeouts, [])

    def test_ws_revoked_token_cannot_issue_next_cancel(self):
        task = self.create_download()

        def revoked_frame():
            user = db.session.get(User, self.user_id)
            user.api_token = None
            db.session.commit()
            return packet(type="download.action", task_id=task["id"], action="cancel")

        sock = self.serve([packet(type="auth", token=self.token), revoked_frame])
        self.assertEqual(sock.closed[0], 1008)
        self.assertEqual(db.session.get(DownloadTask, task["id"]).control, "")


if __name__ == "__main__":
    import unittest
    unittest.main()
