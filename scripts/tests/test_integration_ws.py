"""General WebSocket authentication, isolation and streaming without a network."""
import json
from unittest.mock import patch

from flask import g
from flask_login import current_user
from simple_websocket import ConnectionClosed

from scripts.tests.support import IsolatedAppTestCase

from app.extensions import db
from app.models.conversation import Conversation
from app.models.user import User
from app.services import integration_ws as ws_service
from app.utils.scoping import current_user_id, user_scope


TOKEN = "lx_isolated_websocket_test_token"


def packet(**fields):
    return json.dumps(fields)


class FakeSocket:
    def __init__(self, incoming, fail_on=None):
        self.incoming = iter(incoming)
        self.sent = []
        self.timeouts = []
        self.closed = None
        self.fail_on = fail_on

    def receive(self, timeout=None):
        self.timeouts.append(timeout)
        try:
            item = next(self.incoming)
        except StopIteration:
            raise ConnectionClosed() from None
        return item() if callable(item) else item

    def send(self, data):
        value = json.loads(data)
        if value["type"] == self.fail_on:
            raise ConnectionClosed()
        self.sent.append(value)

    def close(self, reason=None, message=None):
        self.closed = (reason, message)


class IntegrationWebSocketTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        user = self.make_user()
        user.api_token = TOKEN
        other = self.make_user("other_ws_user")
        db.session.commit()
        self.user_id, self.other_id = user.id, other.id
        self.username = user.username
        self.slots = ws_service._ConnectionSlots()

    def serve(self, incoming, url="/api/v1/ws", fail_on=None):
        sock = FakeSocket(incoming, fail_on=fail_on)
        with self.app.test_request_context(url):
            ws_service.serve_connection(sock, self.slots)
        self.assertEqual(self.slots.total, 0)
        self.assertEqual(dict(self.slots.users), {})
        return sock

    def auth(self):
        return packet(type="auth", token=TOKEN)

    def new_conversation(self, owner=None):
        conv = Conversation(user_id=owner or self.user_id, title="测试会话")
        db.session.add(conv)
        db.session.commit()
        return conv.id

    def test_ready_and_ping_echo_only_public_identity(self):
        sock = self.serve([self.auth(), packet(type="ping", request_id="ping-1")])
        self.assertEqual(sock.sent, [
            {"type": "ready", "data": {"user_id": self.user_id,
                "username": self.username, "protocol": "lingxi.v1"}},
            {"type": "pong", "request_id": "ping-1", "data": {}},
        ])
        self.assertNotIn(TOKEN, json.dumps(sock.sent))
        self.assertEqual(sock.timeouts[:2], [10, 25])

    def test_first_frame_requires_valid_auth_even_with_browser_cookie(self):
        invalid = [None, "[]", "{", b'{"type":"auth"}',
                   packet(type="ping"), packet(type="auth", token="wrong"),
                   packet(type="auth", token="bad\ud800"),
                   packet(type="auth", token=4)]
        for first in invalid:
            with self.subTest(first=first):
                sock = self.serve([first])
                self.assertEqual(sock.closed[0], 1008)
                self.assertEqual([v["type"] for v in sock.sent], ["error"])
        with self.app.test_request_context("/api/v1/ws"):
            g._login_user = db.session.get(User, self.user_id)
            sock = FakeSocket([packet(type="auth", token="wrong")])
            ws_service.serve_connection(sock, self.slots)
            self.assertEqual(sock.closed[0], 1008)

    def test_url_token_is_rejected_without_waiting_for_auth(self):
        sock = self.serve([self.auth()], "/api/v1/ws?token=anything")
        self.assertEqual(sock.closed[0], 1008)
        self.assertEqual(sock.timeouts, [])

    def test_invalid_unicode_command_is_rejected_and_connection_remains_usable(self):
        sock = self.serve([self.auth(), packet(type="ping", request_id="bad\ud800"),
                           packet(type="ping", request_id="valid")])
        self.assertEqual(sock.sent[1]["type"], "error")
        self.assertEqual(sock.sent[1]["data"]["code"], "invalid_request")
        self.assertEqual(sock.sent[2]["type"], "pong")
        self.assertEqual(sock.sent[2]["request_id"], "valid")

    def revoke(self, next_frame=None):
        user = db.session.get(User, self.user_id)
        user.api_token = None
        db.session.commit()
        return next_frame

    def test_idle_connection_detects_revocation_within_poll_interval(self):
        sock = self.serve([self.auth(), self.revoke])
        self.assertEqual(sock.closed[0], 1008)
        self.assertEqual(sock.sent[-1]["data"]["code"], "unauthorized")
        self.assertEqual(sock.timeouts, [10, 25])

    def test_revoked_token_cannot_execute_next_command(self):
        send = packet(type="chat.send", request_id="r1", message="/help")
        with patch.object(ws_service, "_handle_chat") as handler:
            sock = self.serve([self.auth(), lambda: self.revoke(send)])
        handler.assert_not_called()
        self.assertEqual(sock.closed[0], 1008)

    def test_chat_stream_orders_events_and_restores_prior_user_scope(self):
        conversation_id = self.new_conversation()
        observed = []

        def chat(conversation, message, user):
            observed.append((conversation.id, user.id, current_user.id, current_user_id()))
            yield "ack", "收到"
            yield "delta", "你好"
            yield "done", "你好"

        with self.app.test_request_context("/api/v1/ws"):
            prior = db.session.get(User, self.other_id)
            g._login_user = prior
            with user_scope(self.other_id), patch(
                "app.services.integration_chat_service.run_chat", side_effect=chat,
            ):
                sock = FakeSocket([self.auth(), packet(
                    type="chat.send", request_id="nav-1",
                    conversation_id=conversation_id, message="你好",
                )])
                ws_service.serve_connection(sock, self.slots)
                self.assertIs(g._login_user, prior)
                self.assertEqual(current_user_id(), self.other_id)
        self.assertEqual(observed, [(conversation_id, self.user_id, self.user_id, self.user_id)])
        events = sock.sent[1:]
        self.assertEqual([e["type"] for e in events], ["start", "ack", "delta", "done"])
        self.assertEqual([e["seq"] for e in events], [1, 2, 3, 4])
        self.assertTrue(all(e["request_id"] == "nav-1" for e in events))
        self.assertTrue(all(e["conversation_id"] == conversation_id for e in events))
        self.assertEqual(self.slots.total, 0)

    def test_foreign_conversation_is_not_read_or_replaced_and_connection_survives(self):
        foreign_id = self.new_conversation(self.other_id)
        with patch("app.services.integration_chat_service.run_chat") as run:
            sock = self.serve([self.auth(), packet(
                type="chat.send", request_id="foreign", conversation_id=foreign_id,
                message="读取会话",
            ), packet(type="ping", request_id="next")])
        run.assert_not_called()
        self.assertEqual(sock.sent[1]["data"]["code"], "not_found")
        self.assertEqual(sock.sent[1]["request_id"], "foreign")
        self.assertEqual(sock.sent[-1]["type"], "pong")
        self.assertEqual(Conversation.query.count(), 1)

    def test_invalid_command_fields_do_not_create_conversations(self):
        frames = ["{}", "[]", "{", '{"type":"ping","request_id":NaN}',
                  packet(type="chat.send", message="/help"),
                  packet(type="chat.send", request_id="x", message="/help", conversation_id=True),
                  packet(type="chat.send", request_id="x", message=""),
                  packet(type="ping", request_id="bad\nrequest")]
        sock = self.serve([self.auth(), *frames, packet(type="ping")])
        self.assertEqual([p["type"] for p in sock.sent[1:-1]], ["error"] * len(frames))
        self.assertEqual(sock.sent[-1]["type"], "pong")
        self.assertEqual(Conversation.query.count(), 0)

    def test_new_chat_uses_local_help_without_live_model(self):
        sock = self.serve([self.auth(), packet(type="chat.send", request_id="help", message="/help")])
        events = sock.sent[1:]
        self.assertEqual(events[0]["type"], "start")
        self.assertEqual(events[-1]["type"], "done")
        self.assertIn("/model", events[-1]["data"])
        conv = Conversation.query.one()
        self.assertEqual(conv.user_id, self.user_id)
        self.assertEqual(len(conv.messages), 2)

    def test_disconnect_closes_chat_generator_and_releases_account_scope(self):
        conversation_id = self.new_conversation()
        finalized = []

        def chat(*args):
            try:
                yield "delta", "hello"
                yield "done", "hello"
            finally:
                finalized.append(True)

        with patch("app.services.integration_chat_service.run_chat", side_effect=chat):
            sock = self.serve([self.auth(), packet(type="chat.send", request_id="disconnect",
                conversation_id=conversation_id, message="hello")], fail_on="delta")
        self.assertEqual(finalized, [True])
        self.assertNotIn("_login_user", g)
        self.assertEqual(current_user_id(), 0)
        self.assertEqual(sock.sent[-1]["type"], "start")

    def test_connection_limits_release_only_the_current_slot(self):
        for _ in range(3):
            self.assertTrue(self.slots.acquire())
            self.assertTrue(self.slots.authenticate(self.user_id))
        with self.app.test_request_context("/api/v1/ws"):
            sock = FakeSocket([self.auth()])
            ws_service.serve_connection(sock, self.slots)
        self.assertEqual(sock.closed[0], 1013)
        self.assertEqual(self.slots.total, 3)
        self.assertEqual(self.slots.users[self.user_id], 3)
        for _ in range(3):
            self.slots.release(self.user_id)
        for _ in range(ws_service.MAX_CONNECTIONS):
            self.assertTrue(self.slots.acquire())
        with self.app.test_request_context("/api/v1/ws"):
            sock = FakeSocket([])
            ws_service.serve_connection(sock, self.slots)
        self.assertEqual(sock.closed[0], 1013)
        self.assertEqual(sock.timeouts, [])
        self.assertEqual(self.slots.total, ws_service.MAX_CONNECTIONS)
        for _ in range(ws_service.MAX_CONNECTIONS):
            self.slots.release()

    def test_transport_limits_are_registered_and_oversize_frame_is_rejected(self):
        ws_service.init_integration_ws(self.app)
        self.assertEqual(self.app.config["SOCK_SERVER_OPTIONS"]["max_message_size"], 512 * 1024)
        self.assertEqual(self.app.config["SOCK_SERVER_OPTIONS"]["ping_interval"], 25)
        routes = [rule for rule in self.app.url_map.iter_rules() if rule.rule == "/api/v1/ws"]
        self.assertEqual(len(routes), 1)
        self.assertTrue(routes[0].websocket)
        with self.assertRaisesRegex(ValueError, "512 KiB"):
            ws_service._parse_frame("x" * (ws_service.MAX_MESSAGE_BYTES + 1))
