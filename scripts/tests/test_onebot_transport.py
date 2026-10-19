"""No-network tests for NapCat authentication, queueing and delivery certainty."""
import json
from threading import Event
import unittest
from unittest.mock import patch

from flask import Flask, current_app
import requests
from simple_websocket import ConnectionClosed

# Initialize application configuration in the isolated fixture environment even
# when this module is run alone, before the app package can load a project .env.
from scripts.tests import support as _isolated_support  # noqa: F401
from app.extensions import db
from app.services import onebot_transport as transport


TOKEN = "isolated-onebot-access-token-1234567890"
BOT_ID = "10001000"
USER_ID = "10002000"


def event(**overrides):
    result = {"post_type": "message", "message_type": "private", "self_id": int(BOT_ID),
              "user_id": int(USER_ID), "message_id": 42,
              "message": [{"type": "text", "data": {"text": "hello"}}]}
    result.update(overrides)
    return json.dumps(result)


class FakeSocket:
    def __init__(self, frames=()):
        self.frames = iter(frames)
        self.closed = None
        self.reads = 0

    def receive(self, timeout=None):
        self.reads += 1
        try:
            item = next(self.frames)
        except StopIteration:
            raise ConnectionClosed() from None
        return item() if callable(item) else item

    def close(self, reason=None, message=None):
        self.closed = (reason, message)


class RecordingRegistry(transport._Registry):
    def __init__(self):
        super().__init__()
        self.accepted = []

    def acquire(self, connection):
        result = super().acquire(connection)
        if result:
            self.accepted.append(connection)
        return result

    def wait(self):
        for connection in self.accepted:
            if connection.worker.ident is not None:
                connection.worker.join(timeout=3)
                if connection.worker.is_alive():
                    raise AssertionError("event worker did not stop")


class FakeResponse:
    def __init__(self, data=None, *, status=200, raw=None):
        self.status_code = status
        self.raw = json.dumps(data).encode() if raw is None else raw
        self.closed = False

    def iter_content(self, chunk_size):
        for index in range(0, len(self.raw), chunk_size):
            yield self.raw[index:index + chunk_size]

    def close(self):
        self.closed = True


def ok(data):
    return FakeResponse({"status": "ok", "retcode": 0, "data": data})


class FakeSession:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []
        self.headers = {}
        self.trust_env = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


class OneBotTransportTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask("onebot-transport-test")
        self.app.config.update(TESTING=True, ONEBOT_ENABLED=True,
                               ONEBOT_ACCESS_TOKEN=TOKEN, ONEBOT_API_URL="http://napcat:3000",
                               SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
        db.init_app(self.app)
        self.registry = RecordingRegistry()
        self.addCleanup(self.registry.wait)

    def serve(self, frames=(), *, headers=None, url="/api/v1/integrations/onebot/ws", wait=True):
        handshake = {"Authorization": "Bearer " + TOKEN, "X-Self-ID": BOT_ID,
                     "X-Client-Role": "Universal"}
        if headers:
            handshake.update(headers)
        socket = FakeSocket(frames)
        with self.app.test_request_context(url, headers=handshake):
            transport.serve_connection(socket, self.registry)
        if wait:
            self.registry.wait()
        return socket

    def send(self, results, *, text="hello", image=None, self_id=BOT_ID, user_id=USER_ID):
        session = FakeSession(results)
        with self.app.app_context(), patch.object(transport.requests, "Session", return_value=session):
            result = transport.send_private(self_id, user_id, text, image)
        return result, session

    def send_error(self, results, *, text="hello", code=None, state="not_sent"):
        session = FakeSession(results)
        with self.app.app_context(), patch.object(transport.requests, "Session", return_value=session):
            with self.assertRaises(transport.OneBotTransportError) as caught:
                transport.send_private(BOT_ID, USER_ID, text)
        error = caught.exception
        self.assertEqual(error.delivery_state, state)
        if code is not None:
            self.assertEqual(error.code, code)
        self.assertNotIn(TOKEN, str(error))
        self.assertNotIn("private server error", str(error))
        return error, session

    def test_disabled_short_and_wrong_tokens_are_rejected_without_reading(self):
        for token in ("", "wrong", TOKEN + "x", "short"):
            with self.subTest(token=token):
                socket = self.serve([event()], headers={"Authorization": "Bearer " + token})
                self.assertEqual(socket.closed[0], 1008)
                self.assertEqual(socket.reads, 0)
        self.app.config["ONEBOT_ACCESS_TOKEN"] = "short"
        socket = self.serve(headers={"Authorization": "Bearer short"})
        self.assertEqual(socket.closed[0], 1008)
        self.app.config.update(ONEBOT_ACCESS_TOKEN=TOKEN, ONEBOT_ENABLED=False)
        self.assertEqual(self.serve().closed[0], 1008)
        self.assertEqual(self.registry.accepted, [])

    def test_query_credentials_are_rejected_even_with_valid_header(self):
        socket = self.serve([event()], url="/api/v1/integrations/onebot/ws?access_token=secret")
        self.assertEqual(socket.closed[0], 1008)
        self.assertEqual(socket.reads, 0)

    def test_headers_require_digit_identity_and_event_capable_role(self):
        for headers in ({"X-Self-ID": ""}, {"X-Self-ID": "+10001"}, {"X-Self-ID": "0001"},
                        {"X-Client-Role": "API"}, {"X-Client-Role": ""}):
            with self.subTest(headers=headers):
                self.assertEqual(self.serve(headers=headers).closed[0], 1008)
        with patch.object(transport, "_dispatch_event") as dispatch:
            self.serve([event()], headers={"X-Client-Role": "Event"})
            dispatch.assert_called_once()

    def test_private_event_gets_application_context_and_other_events_are_ignored(self):
        observed = []

        def dispatch(value):
            observed.append((value["message_id"], current_app.name))

        with patch.object(transport, "_dispatch_event", side_effect=dispatch):
            socket = self.serve([event(post_type="meta_event", meta_event_type="heartbeat"),
                                 event(message_type="group"), event(user_id=int(BOT_ID)),
                                 event(post_type="message_sent"), event()])
        self.assertIsNone(socket.closed)
        self.assertEqual(observed, [(42, self.app.name)])
        self.assertEqual(self.registry.connections, {})

    def test_wrong_event_account_or_sender_cannot_dispatch(self):
        cases = [event(self_id=10003000), event(self_id=True), event(user_id=True),
                 event(sender={"user_id": 10004000}), "[]", "{", b"{}",
                 event(message="bad\ud800"), '{"self_id": NaN}', " " * (transport.MAX_EVENT_BYTES + 1)]
        for frame in cases:
            with self.subTest(frame=repr(frame)[:80]), patch.object(transport, "_dispatch_event") as dispatch:
                self.assertEqual(self.serve([frame]).closed[0], 1008)
                dispatch.assert_not_called()

    def test_revoking_token_while_idle_stops_reader(self):
        def revoke():
            self.app.config["ONEBOT_ACCESS_TOKEN"] = "revoked-and-replaced-token-1234567890"
            return event()

        with patch.object(transport, "_dispatch_event") as dispatch:
            socket = self.serve([revoke])
        self.assertEqual(socket.closed[0], 1008)
        dispatch.assert_not_called()

    def test_duplicate_bot_and_total_connection_limit(self):
        first = transport._Connection(self.app, self.registry, BOT_ID, "Bearer " + TOKEN)
        second = transport._Connection(self.app, self.registry, "10003000", "Bearer " + TOKEN)
        self.assertTrue(self.registry.acquire(first))
        self.assertEqual(self.serve(wait=False).closed[0], 1013)
        self.assertTrue(self.registry.acquire(second))
        self.assertEqual(self.serve(headers={"X-Self-ID": "10004000"}, wait=False).closed[0], 1013)
        self.registry.release(first)
        self.registry.release(second)

    def test_slow_ai_does_not_block_reader_and_queue_is_bounded(self):
        started, release, heartbeat_read = Event(), Event(), Event()
        processed = []

        def dispatch(value):
            processed.append(value["message_id"])
            if value["message_id"] == 1:
                started.set()
                self.assertTrue(release.wait(3))

        def queued_second():
            self.assertTrue(started.wait(3))
            return event(message_id=2)

        def heartbeat():
            heartbeat_read.set()
            return event(post_type="meta_event", meta_event_type="heartbeat")

        with patch.object(transport, "MAX_PENDING_EVENTS", 1), \
                patch.object(transport, "_dispatch_event", side_effect=dispatch):
            try:
                socket = self.serve([event(message_id=1), queued_second, heartbeat, event(message_id=3)], wait=False)
                self.assertTrue(heartbeat_read.is_set())
                self.assertEqual(socket.closed[0], 1013)
                self.assertEqual(self.registry.accepted[0].queue.qsize(), 1)
                # Keep a draining account reserved until accepted tools finish.
                self.assertEqual(self.serve(wait=False).closed[0], 1013)
            finally:
                release.set()
                self.registry.wait()
        self.assertEqual(processed, [1, 2])
        self.assertEqual(self.registry.connections, {})

    def test_worker_failure_is_private_and_does_not_poison_following_event(self):
        with patch.object(transport, "_dispatch_event", side_effect=[RuntimeError(TOKEN), None]) as dispatch, \
                patch.object(self.app.logger, "error") as logger:
            self.serve([event(message_id=1), event(message_id=2)])
        self.assertEqual(dispatch.call_count, 2)
        self.assertNotIn(TOKEN, str(logger.call_args_list))

    def test_init_is_idempotent_and_status_is_application_scoped(self):
        transport.init_onebot(self.app)
        transport.init_onebot(self.app)
        self.assertEqual(sum(rule.rule.endswith("/onebot/ws") for rule in self.app.url_map.iter_rules()), 1)
        registry = self.app.extensions["onebot_transport"]["registry"]
        connection = transport._Connection(self.app, registry, BOT_ID, "Bearer " + TOKEN)
        registry.acquire(connection)
        try:
            with self.app.app_context():
                self.assertEqual(transport.connection_status(), {"enabled": True, "connected": True, "self_id": BOT_ID})
            other_app = Flask("other-onebot-app")
            other_app.config["ONEBOT_ENABLED"] = True
            transport.init_onebot(other_app)
            with other_app.app_context():
                self.assertEqual(transport.connection_status(), {"enabled": True, "connected": False, "self_id": None})
            connection.stop()
            with self.app.app_context():
                self.assertFalse(transport.connection_status()["connected"])
        finally:
            registry.release(connection)

    def test_send_verifies_account_and_sends_cq_as_literal_text(self):
        text = "literal [CQ:image,file=http://invalid.example/private]"
        login, sent = ok({"user_id": int(BOT_ID)}), ok({"message_id": -123})
        result, session = self.send([login, sent], text=text, image=b"sample image bytes")
        self.assertEqual(result, {"ok": True, "message_ids": [-123]})
        self.assertFalse(session.trust_env)
        self.assertEqual(session.headers["Authorization"], "Bearer " + TOKEN)
        self.assertTrue(login.closed and sent.closed)
        self.assertEqual([url for url, kwargs in session.calls],
                         ["http://napcat:3000/get_login_info", "http://napcat:3000/send_private_msg"])
        payload = session.calls[1][1]["json"]
        self.assertEqual(payload["user_id"], int(USER_ID))
        self.assertEqual(payload["message"][0], {"type": "text", "data": {"text": text}})
        self.assertTrue(payload["message"][1]["data"]["file"].startswith("base64://"))
        for _url, kwargs in session.calls:
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["timeout"], transport.HTTP_TIMEOUT)

    def test_mismatched_login_identity_never_sends(self):
        for identity in (10003000, True, None, "bad"):
            with self.subTest(identity=identity):
                error, session = self.send_error([ok({"user_id": identity})], code="account_mismatch")
                self.assertEqual(len(session.calls), 1)

    def test_failed_login_never_attempts_send(self):
        for response in (requests.Timeout("private server error"), FakeResponse(raw=b"not json"),
                         FakeResponse(status=302), FakeResponse({"status": "failed", "retcode": 403})):
            with self.subTest(response=type(response).__name__):
                error, session = self.send_error([response])
                self.assertEqual(len(session.calls), 1)

    def test_network_http_and_malformed_response_are_unknown_without_retry(self):
        cases = [requests.Timeout("private server error " + TOKEN), requests.ConnectionError(TOKEN),
                 FakeResponse(status=500), FakeResponse(status=302), FakeResponse(raw=b"not json"),
                 FakeResponse(raw=b"x" * (transport.MAX_RESPONSE_BYTES + 1)),
                 FakeResponse({"status": "async", "retcode": 1}),
                 FakeResponse({"status": "ok", "retcode": False, "data": {"message_id": 1}}),
                 ok({}), ok({"message_id": True}), ok({"message_id": "123"})]
        for response in cases:
            with self.subTest(response=type(response).__name__):
                error, session = self.send_error([ok({"user_id": int(BOT_ID)}), response], state="unknown")
                self.assertTrue(error.uncertain)
                self.assertEqual(len(session.calls), 2)

    def test_explicit_api_rejection_is_not_reported_as_delivery(self):
        failed = FakeResponse({"status": "failed", "retcode": 1200, "message": "private server error"})
        error, session = self.send_error([ok({"user_id": int(BOT_ID)}), failed], code="api_rejected")
        self.assertFalse(error.uncertain)
        self.assertEqual(error.message_ids, [])
        self.assertEqual(len(session.calls), 2)

    def test_long_text_splits_and_partial_send_preserves_confirmation(self):
        text = "a" * (transport.TEXT_CHUNK_CHARS + 1)
        error, session = self.send_error([ok({"user_id": int(BOT_ID)}), ok({"message_id": 123}),
                                          requests.Timeout("private server error")], text=text, state="partial")
        self.assertEqual(error.message_ids, [123])
        self.assertTrue(error.uncertain)
        self.assertEqual(len(session.calls), 3)
        chunks = [call[1]["json"]["message"][0]["data"]["text"] for call in session.calls[1:]]
        self.assertEqual("".join(chunks), text)

    def test_invalid_payload_and_untrusted_url_shape_fail_before_http(self):
        with self.app.app_context(), patch.object(transport.requests, "Session") as session:
            cases = [(True, USER_ID, "hi", None), (BOT_ID, BOT_ID, "hi", None),
                     (BOT_ID, USER_ID, "", None), (BOT_ID, USER_ID, "bad\ud800", None),
                     (BOT_ID, USER_ID, "x" * (transport.MAX_TEXT_CHARS + 1), None),
                     (BOT_ID, USER_ID, "hi", b""), (BOT_ID, USER_ID, "hi", "http://image")]
            for args in cases:
                with self.subTest(args=repr(args)[:50]), self.assertRaises(transport.OneBotTransportError):
                    transport.send_private(*args)
            for url in ("file:///etc/passwd", "http://user:password@napcat:3000", "http://napcat:3000?token=secret",
                        "http://napcat:3000/#fragment", "http://napcat:999999", "http://napcat\n/", ""):
                self.app.config["ONEBOT_API_URL"] = url
                with self.subTest(url=url), self.assertRaises(transport.OneBotTransportError):
                    transport.send_private(BOT_ID, USER_ID, "hello")
            session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
