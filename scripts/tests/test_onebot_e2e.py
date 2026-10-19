"""Real loopback WebSocket -> Lingxi -> fake NapCat HTTP protocol smoke.

Uses a temporary SQLite file and the isolated test configuration, never project
credentials, live QQ, an LLM, or a remote endpoint. Optional test-only client:
    python -m pip install websocket-client
    python -m unittest scripts.tests.test_onebot_e2e -v
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import tempfile
from threading import Condition, Thread
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import requests
from werkzeug.serving import WSGIRequestHandler, make_server

# Import the isolated config before importing any application module. This keeps
# Config's optional env generation and loading away from the project .env.
from scripts.tests.support import make_app
from app.extensions import db
from app.models.conversation import Message
from app.models.notification import Notification
from app.models.onebot import OneBotBinding, OneBotBindingCode, OneBotEvent
from app.models.setting import Setting
from app.models.user import User

try:
    import websocket
except ImportError:
    websocket = None


TOKEN = "isolated-e2e-napcat-token-123456789012345"
BOT_ID, USER_ID = 10001000, 10002000
PASSWORD = "isolated-e2e-password"


class QuietRequestHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        pass


class FakeNapCat:
    def __init__(self):
        self.condition = Condition()
        self.messages = []
        self.actions = []
        self.errors = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args, **kwargs):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                with fixture.condition:
                    if self.headers.get("Authorization") != "Bearer " + TOKEN:
                        fixture.errors.append("invalid Authorization")
                        status, data = 401, {"status": "failed", "retcode": 401}
                    elif self.path == "/get_login_info":
                        fixture.actions.append(self.path)
                        status, data = 200, {"status": "ok", "retcode": 0, "data": {"user_id": BOT_ID}}
                    elif self.path == "/send_private_msg":
                        fixture.actions.append(self.path)
                        fixture.messages.append(payload)
                        status, data = 200, {"status": "ok", "retcode": 0,
                                             "data": {"message_id": 1000 + len(fixture.messages)}}
                    else:
                        fixture.errors.append("unexpected action")
                        status, data = 404, {"status": "failed", "retcode": 404}
                    body = json.dumps(data).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
                    fixture.condition.notify_all()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def wait_for_messages(self, count):
        with self.condition:
            if not self.condition.wait_for(lambda: len(self.messages) >= count, timeout=8):
                raise AssertionError(f"Expected {count} fake deliveries, got {len(self.messages)}")
            return list(self.messages)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


@unittest.skipIf(websocket is None, "websocket-client is required for loopback protocol smoke")
class OneBotLoopbackTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lingxi-onebot-e2e-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.napcat = FakeNapCat()
        self.addCleanup(self.napcat.close)
        self.app = make_app(
            SQLALCHEMY_DATABASE_URI="sqlite:///" + (root / "test.sqlite3").as_posix(),
            SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 10}},
            ADMIN_ENTRY="lingxi", ONEBOT_ENABLED=True, ONEBOT_ACCESS_TOKEN=TOKEN,
            ONEBOT_API_URL=self.napcat.url, ONEBOT_WEBUI_URL="", DATA_DIR=root,
            IMAGE_DIR=root / "images", BACKUP_DIR=root / "backups", PAGE_HOST="127.0.0.1",
            PAGE_PUBLIC_BASE_URL="",
        )
        with self.app.app_context():
            self.assertEqual(db.engine.url.get_backend_name(), "sqlite")
            self.assertEqual(Path(db.engine.url.database).resolve().parent, root.resolve())
            db.create_all()
            user = User(username="qq_e2e_admin", is_admin=True, timezone="Asia/Shanghai")
            user.set_password(PASSWORD)
            db.session.add(user)
            db.session.commit()
            self.user_id = user.id
        self.addCleanup(self.dispose_database)
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True, request_handler=QuietRequestHandler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_app_server)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.base = self.origin + "/lingxi"
        self.session = requests.Session()
        self.session.trust_env = False
        self.addCleanup(self.session.close)
        self.sockets = []
        self.addCleanup(self.close_sockets)

        # Restrict even accidental requests to these two disposable fixtures.
        real_request = requests.sessions.Session.request
        allowed_ports = {self.server.server_port, self.napcat.server.server_port}

        def loopback_only(session, method, url, *args, **kwargs):
            parsed = urlsplit(url)
            if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port not in allowed_ports:
                raise AssertionError("External HTTP disabled in OneBot smoke")
            return real_request(session, method, url, *args, **kwargs)

        for target, options in (
            ("requests.sessions.Session.request", {"new": loopback_only}),
            ("httpx.Client.send", {"side_effect": AssertionError("External model HTTP disabled")}),
            ("app.ai.executor.LLMClient", {"side_effect": AssertionError("/help must not construct an LLM")}),
            ("app.services.feishu_ws.sdk_available", {"return_value": False}),
            ("app.utils.netinfo.diagnose_page_reachability", {"return_value": {}}),
        ):
            guard = patch(target, **options)
            guard.start()
            self.addCleanup(guard.stop)

    def dispose_database(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def close_app_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def close_sockets(self):
        registry = self.app.extensions["onebot_transport"]["registry"]
        with registry.lock:
            connections = list(registry.connections.values())
        for socket in self.sockets:
            socket.close(timeout=2)
        for connection in connections:
            connection.worker.join(timeout=5)
            self.assertFalse(connection.worker.is_alive(), "OneBot worker did not drain after disconnect")

    def connect(self, *, token=TOKEN, prefix="/lingxi"):
        socket = websocket.create_connection(
            f"ws://127.0.0.1:{self.server.server_port}{prefix}/api/v1/integrations/onebot/ws",
            header={"Authorization": "Bearer " + token, "X-Self-ID": str(BOT_ID),
                    "X-Client-Role": "Universal"}, timeout=5, http_no_proxy=["127.0.0.1"],
        )
        self.sockets.append(socket)
        return socket

    def send_event(self, socket, message_id, text):
        packet = {"time": time.time(), "self_id": BOT_ID, "post_type": "message",
                  "message_type": "private", "sub_type": "friend", "user_id": USER_ID,
                  "message_id": message_id,
                  "message": [{"type": "text", "data": {"text": text}}]}
        socket.send(json.dumps(packet, ensure_ascii=False))

    def csrf(self, html):
        match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html)
        self.assertIsNotNone(match, "response has no CSRF field")
        return match.group(1)

    def login(self):
        page = self.session.get(self.base + "/login", timeout=5)
        self.assertEqual(page.status_code, 200)
        result = self.session.post(self.base + "/login", data={
            "username": "qq_e2e_admin", "password": PASSWORD, "csrf_token": self.csrf(page.text),
        }, allow_redirects=False, timeout=5)
        self.assertEqual(result.status_code, 302)

    @staticmethod
    def text(payload):
        return "".join(part["data"].get("text", "") for part in payload["message"] if part["type"] == "text")

    def wait_until_drained(self):
        registry = self.app.extensions["onebot_transport"]["registry"]
        with registry.lock:
            queue = registry.connections[str(BOT_ID)].queue
        with queue.all_tasks_done:
            self.assertTrue(queue.all_tasks_done.wait_for(lambda: queue.unfinished_tasks == 0, timeout=8))

    def test_real_prefixed_ws_binding_help_deduplication_and_notification(self):
        self.login()
        page = self.session.get(self.base + "/settings/qq/", timeout=5)
        self.assertEqual(page.status_code, 200)
        generated = self.session.post(self.base + "/settings/qq/code", data={
            "csrf_token": self.csrf(page.text),
        }, timeout=5)
        self.assertEqual(generated.status_code, 200)
        code = re.search(r"/lingxi 绑定 ([0-9A-F]{24})", generated.text).group(1)
        self.assertNotIn(TOKEN, generated.text)
        socket = self.connect()
        self.send_event(socket, 10, "/lingxi 绑定 " + code)
        first = self.napcat.wait_for_messages(1)
        self.assertIn("已绑定", self.text(first[0]))
        self.send_event(socket, 11, "/help")
        second = self.napcat.wait_for_messages(2)
        self.assertIn("/model", self.text(second[1]))
        self.assertIn("/compact", self.text(second[1]))

        # A subsequent status reply acts as a serial queue barrier: the replay
        # before it has been processed, without using a timing-based sleep.
        self.send_event(socket, 11, "/help")
        self.send_event(socket, 12, "/lingxi 状态")
        third = self.napcat.wait_for_messages(3)
        self.assertIn("当前 QQ 私聊已绑定", self.text(third[2]))
        self.wait_until_drained()
        self.assertEqual(len(self.napcat.messages), 3)
        with self.app.app_context():
            binding = OneBotBinding.query.one()
            self.assertEqual((binding.user_id, binding.self_id, binding.qq_user_id),
                             (self.user_id, str(BOT_ID), str(USER_ID)))
            self.assertEqual(OneBotBindingCode.query.count(), 0)
            self.assertEqual(OneBotEvent.query.count(), 3)
            self.assertTrue(all(row.status == "done" for row in OneBotEvent.query.all()))
            self.assertEqual(Message.query.filter_by(conversation_id=binding.conversation_id, role="user").count(), 1)
            self.assertEqual(Message.query.filter_by(conversation_id=binding.conversation_id, role="assistant").count(), 1)

        page = self.session.get(self.base + "/settings/qq/", timeout=5)
        self.assertIn("QQ 已连接", page.text)
        self.assertIn("账号已绑定", page.text)
        saved = self.session.post(self.base + "/settings/channels", data={
            "csrf_token": self.csrf(page.text), "default_channels": "onebot",
            "scene_briefing": "onebot", "scene_report": "onebot", "scene_backup": "onebot",
            "scene_cleanup": "onebot", "scene_reminder": "onebot",
        }, allow_redirects=False, timeout=5)
        self.assertEqual(saved.status_code, 302)
        with self.app.app_context():
            for key in ("default_channels", "notify_briefing_channels", "notify_report_channels",
                        "notify_backup_channels", "notify_cleanup_channels", "notify_reminder_channels"):
                row = Setting.query.filter_by(user_id=self.user_id, key=key).one()
                self.assertEqual(row.value, ["onebot"], key + " discarded the OneBot channel")
                self.assertIsNone(Setting.query.filter_by(user_id=0, key=key).first())
        settings_page = self.session.get(self.base + "/settings/?tab=channels", timeout=5)
        self.assertEqual(settings_page.status_code, 200)
        self.assertRegex(settings_page.text, r'name="default_channels" value="onebot"\s+checked')
        notification = self.session.post(self.base + "/settings/api/test-channel",
                                         json={"channel": "onebot"}, headers={"X-CSRFToken": self.csrf(page.text)}, timeout=5)
        self.assertEqual(notification.status_code, 200, notification.text)
        self.assertEqual(notification.json()["data"]["status"], "sent")
        fourth = self.napcat.wait_for_messages(4)
        self.assertIn("测试通知", self.text(fourth[3]))
        self.assertEqual(self.napcat.errors, [])
        self.assertEqual(self.napcat.actions, ["/get_login_info", "/send_private_msg"] * 4)
        self.assertTrue(all(message["user_id"] == USER_ID for message in fourth))
        with self.app.app_context():
            delivered = Notification.query.filter_by(channel="onebot").one()
            self.assertEqual(delivered.user_id, self.user_id)
            self.assertEqual(delivered.status, "sent")
            self.assertIsNotNone(delivered.sent_at)

    def test_real_upgrade_requires_admin_prefix_and_authorized_header(self):
        with self.assertRaises(websocket.WebSocketBadStatusException) as rejected:
            self.connect(prefix="")
        self.assertEqual(rejected.exception.status_code, 404)
        socket = self.connect(token="not-the-registered-token-123456789")
        opcode, frame = socket.recv_data(control_frame=True)
        self.assertEqual(opcode, websocket.ABNF.OPCODE_CLOSE)
        self.assertEqual(int.from_bytes(frame[:2], "big"), 1008)
        self.assertEqual(self.napcat.messages, [])


if __name__ == "__main__":
    unittest.main()
