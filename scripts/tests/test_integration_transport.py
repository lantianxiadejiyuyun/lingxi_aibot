"""Real loopback HTTP/WebSocket transport; no production or model calls."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from simple_websocket import Client, ConnectionClosed
from werkzeug.serving import WSGIRequestHandler, make_server

from scripts.tests.support import make_app
from app.extensions import db
from app.models.user import User


class _QuietHandler(WSGIRequestHandler):
    def log(self, _type, message, *args):
        pass


class IntegrationTransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lingxi-loopback-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.app = make_app(
            ADMIN_ENTRY="lingxi",
            SQLALCHEMY_DATABASE_URI="sqlite:///" + str(root / "transport.sqlite").replace("\\", "/"),
            DATA_DIR=root, IMAGE_DIR=root / "images", BACKUP_DIR=root / "backups",
        )
        self.addCleanup(self._close_database)
        self.token = "lx_loopback_transport_test_token"
        with self.app.app_context():
            db.create_all()
            user = User(username="loopback_user", timezone="Asia/Shanghai", api_token=self.token)
            user.set_password("isolated-loopback-password")
            db.session.add(user)
            db.session.commit()
            self.user_id = user.id
        for target in ("requests.sessions.Session.request", "httpx.Client.send"):
            blocker = patch(target, side_effect=AssertionError("External HTTP disabled in loopback tests"))
            blocker.start()
            self.addCleanup(blocker.stop)
        self.http = build_opener(ProxyHandler({}))
        self.sockets = []
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True, request_handler=_QuietHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self._close_server)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.base = self.origin + "/lingxi/api/v1"
        self.ws_url = f"ws://127.0.0.1:{self.server.server_port}/lingxi/api/v1/ws"

    def _close_database(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def _close_server(self):
        for client in self.sockets:
            try:
                client.close()
            except (ConnectionClosed, OSError):
                pass
            finally:
                # Unblock the receiver even when the peer closed first.
                try:
                    client.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                client.sock.close()
                client.thread.join(timeout=3)
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=3)
        self.assertFalse(self.server_thread.is_alive(), "Loopback server failed to stop")
        for client in self.sockets:
            self.assertFalse(client.thread.is_alive(), "WebSocket receiver failed to stop")

    def http_request(self, path, *, token=True, body=None, raw=False, absolute=False):
        headers = {"Authorization": "Bearer " + self.token} if token else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + path if absolute else self.base + path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers=headers)
        try:
            response = self.http.open(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            payload = response.read().decode("utf-8")
            return response.status, payload if raw else json.loads(payload), response.headers

    def connect(self, token=None):
        client = Client.connect(self.ws_url)
        self.sockets.append(client)
        client.send(json.dumps({"type": "auth", "token": self.token if token is None else token}))
        return client

    def receive(self, client):
        payload = client.receive(timeout=5)
        self.assertIsNotNone(payload, "WebSocket event timed out")
        return json.loads(payload)

    def test_actual_upgrade_chat_stream_and_http_history_share_account(self):
        status, identity, headers = self.http_request("/me")
        self.assertEqual(status, 200)
        self.assertEqual(identity["data"]["id"], self.user_id)
        self.assertEqual(identity["data"]["websocket_path"], "/lingxi/api/v1/ws")
        self.assertNotIn("Set-Cookie", headers)
        client = self.connect()
        ready = self.receive(client)
        self.assertEqual(ready["type"], "ready")
        self.assertEqual(ready["data"]["user_id"], self.user_id)
        client.send(json.dumps({"type": "ping", "request_id": "loop-ping"}))
        self.assertEqual(self.receive(client), {"type": "pong", "request_id": "loop-ping", "data": {}})
        client.send(json.dumps({"type": "chat.send", "message": "/help", "request_id": "loop-chat"}))
        events = []
        for _ in range(20):
            event = self.receive(client)
            events.append(event)
            if event["type"] == "done":
                break
        self.assertEqual(events[0]["type"], "start")
        self.assertEqual(events[-1]["type"], "done")
        self.assertIn("delta", [event["type"] for event in events])
        self.assertNotIn("error", [event["type"] for event in events])
        self.assertIn("/model", events[-1]["data"])
        self.assertEqual([event["seq"] for event in events], list(range(1, len(events) + 1)))
        self.assertTrue(all(event["request_id"] == "loop-chat" for event in events))
        conversation_id = events[0]["conversation_id"]
        status, history, _ = self.http_request(f"/conversations/{conversation_id}/messages")
        self.assertEqual(status, 200)
        self.assertEqual([message["role"] for message in history["data"]], ["user", "assistant"])
        self.assertEqual(history["data"][0]["content"], "/help")
        self.assertEqual(history["data"][1]["content"], events[-1]["data"])

        # The same conversation also works over real POST/SSE through the prefix.
        status, stream, headers = self.http_request(f"/conversations/{conversation_id}/messages",
                                                  body={"message": "/help", "stream": True,
                                                        "request_id": "loop-sse"}, raw=True)
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["Content-Type"])
        self.assertEqual(headers["X-Accel-Buffering"], "no")
        sse_events = [json.loads(line[len("data: "):]) for line in stream.splitlines()
                      if line.startswith("data: ")]
        self.assertEqual(sse_events[0]["type"], "start")
        self.assertEqual(sse_events[-1]["type"], "done")
        self.assertTrue(all(event["conversation_id"] == conversation_id for event in sse_events))

    def test_real_socket_rejects_bad_token_and_closes_after_revocation(self):
        invalid = self.connect(token="wrong-token")
        self.assertEqual(self.receive(invalid)["data"]["code"], "unauthorized")
        with self.assertRaises(ConnectionClosed):
            invalid.receive(timeout=5)
        self.assertEqual(invalid.close_reason, 1008)

        client = self.connect()
        self.assertEqual(self.receive(client)["type"], "ready")
        with self.app.app_context():
            user = db.session.get(User, self.user_id)
            user.api_token = None
            db.session.commit()
        client.send(json.dumps({"type": "ping", "request_id": "after-revoke"}))
        error = self.receive(client)
        self.assertEqual(error["type"], "error")
        self.assertEqual(error["data"]["code"], "unauthorized")
        with self.assertRaises(ConnectionClosed):
            client.receive(timeout=5)
        self.assertEqual(client.close_reason, 1008)
        self.assertEqual(self.http_request("/me")[0], 401)

    def test_http_prefix_and_bearer_are_required_on_real_transport(self):
        status, body, _ = self.http_request("/api/v1/me", absolute=True, raw=True)
        self.assertEqual(status, 404)
        self.assertEqual(body, "Not Found")
        self.assertEqual(self.http_request("/me", token=False)[0], 401)
        status, body, _ = self.http_request("/api/v1/ws", absolute=True, raw=True)
        self.assertEqual(status, 404)
        self.assertEqual(body, "Not Found")
