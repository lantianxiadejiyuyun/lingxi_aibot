"""Web SSE lifecycle and conversation identity, without external models."""
import json
from unittest.mock import patch

from scripts.tests.support import IsolatedAppTestCase
from app.extensions import db
from app.models.conversation import Conversation


def parse_events(response):
    events = []
    for block in response.get_data(as_text=True).split("\n\n"):
        if not block.strip():
            continue
        kind, *data = block.splitlines()
        events.append((kind.removeprefix("event: "),
                       "\n".join(line.removeprefix("data: ") for line in data)))
    return events


class WebChatStreamTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.user = self.make_user()
        self.client = self.client_for(self.user)
        self.conversation = Conversation(user_id=self.user.id, title="existing")
        db.session.add(self.conversation)
        db.session.commit()

    def send(self, **payload):
        return self.client.post("/chat/api/send", json={
            "message": "/help", "conversation_id": self.conversation.id, **payload,
        })

    def test_new_conversation_id_is_sent_before_execution(self):
        with patch("app.blueprints.chat.run_chat", return_value=iter([
            ("delta", "hello"), ("done", "hello"),
        ])):
            response = self.send(conversation_id=None)
            self.assertEqual(response.mimetype, "text/event-stream")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["X-Accel-Buffering"], "no")
            events = parse_events(response)
        self.assertEqual([kind for kind, _ in events], ["start", "delta", "done"])
        identifier = json.loads(events[0][1])["conversation_id"]
        self.assertNotEqual(identifier, self.conversation.id)
        self.assertEqual(db.session.get(Conversation, identifier).user_id, self.user.id)

    def test_invalid_messages_do_not_create_conversations(self):
        count = Conversation.query.count()
        for body in (None, [], {}, {"message": 3}, {"message": " "},
                     {"message": "x" * 100001}, {"message": "\ud800"}):
            with self.subTest(body=repr(body)[:50]):
                response = self.client.post("/chat/api/send", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["ok"])
        self.assertEqual(Conversation.query.count(), count)

    def test_missing_foreign_and_invalid_ids_never_create_replacement_conversation(self):
        foreign = Conversation(user_id=self.user.id + 1, title="foreign")
        db.session.add(foreign)
        db.session.commit()
        count = Conversation.query.count()
        for identifier, status in ((foreign.id, 404), (9999, 404), ("bad", 400),
                                   (True, 400), (0, 400), (1.5, 400), (2**63, 400)):
            with self.subTest(identifier=identifier):
                response = self.send(conversation_id=identifier)
                self.assertEqual(response.status_code, status)
        self.assertEqual(Conversation.query.count(), count)

    def test_sidebar_string_id_and_missing_model_have_terminal_done(self):
        response = self.send(conversation_id=str(self.conversation.id), message="hello")
        events = parse_events(response)
        self.assertEqual([kind for kind, _ in events], ["start", "error", "done"])
        self.assertIn("未配置", events[1][1])
        self.assertEqual(events[-1][1], "")

    def test_unexpected_end_and_exceptions_are_errors_with_one_done(self):
        def broken(*args):
            yield "delta", "partial"
            raise RuntimeError("private-provider-details")

        for executor in (lambda *args: iter([("delta", "partial")]), broken):
            with self.subTest(executor=executor), patch("app.blueprints.chat.run_chat", executor):
                events = parse_events(self.send())
            self.assertEqual([kind for kind, _ in events], ["start", "delta", "error", "done"])
            self.assertEqual(events[-1][1], "")
            self.assertNotIn("private-provider-details", str(events))

    def test_terminal_done_closes_executor_and_ignores_trailing_events(self):
        closed = []

        def execute(*args):
            try:
                yield "ack", "收到"
                yield "delta", "answer"
                yield "done", "收到\n\nanswer"
                yield "delta", "must never be shown"
            finally:
                closed.append(True)

        with patch("app.blueprints.chat.run_chat", execute):
            events = parse_events(self.send())
        self.assertEqual(closed, [True])
        self.assertEqual([kind for kind, _ in events], ["start", "delta", "delta", "done"])
        self.assertEqual(events[-1][1], "收到\n\nanswer")
        self.assertEqual(events[1][1] + events[2][1], events[-1][1])

    def test_client_disconnect_closes_executor(self):
        closed = []

        def execute(*args):
            try:
                yield "delta", "partial"
                yield "done", "partial"
            finally:
                closed.append(True)

        with patch("app.blueprints.chat.run_chat", execute):
            response = self.send()
            stream = iter(response.response)
            self.assertIn(b"event: start", next(stream))
            self.assertIn(b"event: delta", next(stream))
            response.close()
        self.assertEqual(closed, [True])
