"""No live platform traffic: binding, queued delivery and account isolation."""
from datetime import timedelta

from scripts.tests.support import IsolatedAppTestCase
from app.extensions import db
from app.models.astrbot import AstrBotBinding, AstrBotOutbox
from app.models.conversation import Conversation, Message
from app.models.notification import Notification
from app.services import astrbot_service as bridge
from app.services.notify_service import notify
from app.utils.integration_api import ApiError
from app.utils.scoping import user_scope
from app.utils.timeutil import utcnow

BASE = "/api/v1/integrations/astrbot"
SESSION = "feishu-main:FriendMessage:owner-42"


class AstrBotBridgeTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.other = self.make_user("bridge-other", is_admin=False)
        self.user.api_token = "bridge-owner-token"
        self.other.api_token = "bridge-other-token"
        db.session.commit()
        self.client = self.app.test_client()
        self.headers = {"Authorization": "Bearer bridge-owner-token"}
        self.other_headers = {"Authorization": "Bearer bridge-other-token"}

    def queue(self, body="真实的主动消息", user=None):
        user = user or self.user
        with user_scope(user.id):
            return notify("灵犀", body, ["astrbot"], user_id=user.id)[0]

    def test_token_required_even_with_browser_login_and_csrf_exempt_for_token(self):
        client = self.client_for(self.user)
        self.assertEqual(client.get(BASE + "/binding").status_code, 401)
        result = client.put(BASE + "/binding", json={"session": SESSION}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json["data"]["bound"])
        self.assertEqual(client.post(BASE + "/outbox/claim", json={}).status_code, 401)

    def test_binding_private_only_and_idempotent(self):
        for value in ("", "bot:GroupMessage:group", "bot:FriendMessage:", "bot:FriendMessage:a\n", "a" * 513, None):
            result = self.client.put(BASE + "/binding", json={"session": value}, headers=self.headers)
            self.assertEqual(result.status_code, 400)
        first = bridge.bind(self.user.id, SESSION)
        second = bridge.bind(self.user.id, SESSION)
        self.assertEqual(first["conversation_id"], second["conversation_id"])
        self.assertEqual(Conversation.query.filter_by(user_id=self.user.id).count(), 1)

    def test_missing_conversation_is_repaired_only_for_owner(self):
        original = bridge.bind(self.user.id, SESSION)
        Conversation.query.filter_by(id=original["conversation_id"]).delete()
        db.session.commit()
        self.assertIsNone(bridge.binding_view(self.user.id)["conversation_id"])
        result = bridge.bind(self.user.id, SESSION)
        self.assertIsNotNone(result["conversation_id"])
        self.assertFalse(bridge.binding_view(self.other.id)["bound"])

    def test_notification_is_pending_until_ack_and_ack_is_idempotent(self):
        bridge.bind(self.user.id, SESSION)
        record = self.queue()
        self.assertEqual(record.status, "pending")
        self.assertIsNone(record.sent_at)
        message = bridge.claim(self.user.id)[0]
        self.assertEqual(message["session"], SESSION)
        self.assertEqual(message["text"], "灵犀\n\n真实的主动消息")
        self.assertEqual(bridge.claim(self.user.id), [])
        for _ in range(2):
            result = bridge.acknowledge(self.user.id, message["id"], message["lease_token"], "sent")
            self.assertEqual(result["status"], "sent")
        db.session.refresh(record)
        self.assertEqual(record.status, "sent")
        self.assertIsNotNone(record.sent_at)
        self.assertEqual(bridge.claim(self.user.id), [])
        self.assertEqual(Message.query.count(), 1)
        self.assertEqual(Message.query.one().role, "assistant")
        self.assertEqual(Message.query.one().content, message["text"])

    def test_account_isolation_and_no_implicit_binding(self):
        bridge.bind(self.user.id, SESSION)
        record = self.queue()
        self.assertEqual(bridge.claim(self.other.id), [])
        message = bridge.claim(self.user.id)[0]
        result = self.client.post(BASE + f"/outbox/{message['id']}/ack",
            json={"lease_token": message["lease_token"], "outcome": "sent"}, headers=self.other_headers)
        self.assertEqual(result.status_code, 404)
        other_record = self.queue(user=self.other)
        self.assertEqual(other_record.status, "failed")
        self.assertEqual(AstrBotOutbox.query.count(), 1)
        db.session.refresh(record)
        self.assertEqual(record.status, "pending")

    def test_rebinding_and_unbinding_cancel_old_routes(self):
        first = bridge.bind(self.user.id, SESSION)
        record = self.queue()
        message = bridge.claim(self.user.id)[0]
        second = bridge.bind(self.user.id, "feishu-main:FriendMessage:owner-new")
        self.assertNotEqual(first["conversation_id"], second["conversation_id"])
        self.assertEqual(bridge.claim(self.user.id), [])
        db.session.refresh(record)
        self.assertEqual(record.status, "failed")
        with self.assertRaises(ApiError):
            bridge.acknowledge(self.user.id, message["id"], message["lease_token"], "sent")
        db.session.rollback()
        self.queue()
        bridge.unbind(self.user.id)
        self.assertFalse(bridge.binding_view(self.user.id)["bound"])
        self.assertEqual(AstrBotOutbox.query.filter_by(status="pending").count(), 0)
        self.assertEqual(Conversation.query.filter_by(user_id=self.user.id).count(), 2)

    def test_expired_lease_can_be_reclaimed_and_old_ack_rejected(self):
        bridge.bind(self.user.id, SESSION)
        self.queue()
        now = utcnow()
        first = bridge.claim(self.user.id, now=now)[0]
        second = bridge.claim(self.user.id, now=now + timedelta(seconds=301))[0]
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["lease_token"], second["lease_token"])
        with self.assertRaises(ApiError):
            bridge.acknowledge(self.user.id, first["id"], first["lease_token"], "sent")
        db.session.rollback()
        bridge.acknowledge(self.user.id, second["id"], second["lease_token"], "sent")

    def test_failed_delivery_backs_off_and_stops_after_five_claims(self):
        bridge.bind(self.user.id, SESSION)
        record = self.queue()
        for attempt in range(5):
            row = AstrBotOutbox.query.one()
            message = bridge.claim(self.user.id, now=max(utcnow(), row.available_at))[0]
            bridge.acknowledge(self.user.id, message["id"], message["lease_token"], "failed")
            if attempt < 4:
                self.assertEqual(bridge.claim(self.user.id), [])
        self.assertEqual(AstrBotOutbox.query.one().status, "failed")
        db.session.refresh(record)
        self.assertEqual(record.status, "failed")

    def test_expired_notification_never_delivers_next_day(self):
        bridge.bind(self.user.id, SESSION)
        record = self.queue()
        self.assertEqual(bridge.claim(self.user.id, now=utcnow() + timedelta(hours=25)), [])
        db.session.refresh(record)
        self.assertEqual(record.status, "failed")

    def test_claim_limit_and_ack_payload_are_strict(self):
        for limit in (0, 11, True, "2", None):
            result = self.client.post(BASE + "/outbox/claim", json={"limit": limit}, headers=self.headers)
            self.assertEqual(result.status_code, 400)
        result = self.client.post(BASE + "/outbox/claim", json={"extra": "unknown"}, headers=self.headers)
        self.assertEqual(result.status_code, 400)
        for payload in ({}, {"lease_token": [], "outcome": "sent"}, {"lease_token": "x", "outcome": {}},
                        {"lease_token": "中文", "outcome": "sent"}):
            result = self.client.post(BASE + "/outbox/1/ack", json=payload, headers=self.headers)
            self.assertEqual(result.status_code, 400)

    def test_stale_plugin_cannot_unbind_a_new_private_session(self):
        bridge.bind(self.user.id, "new:FriendMessage:peer")
        result = self.client.delete(BASE + "/binding", json={"session": SESSION}, headers=self.headers)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(bridge.binding_view(self.user.id)["session"], "new:FriendMessage:peer")
        result = self.client.delete(BASE + "/binding", json={"session": "new:FriendMessage:peer"}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json["data"]["bound"])

    def test_companion_queue_honors_quiet_hours_then_cancel_when_disabled(self):
        from datetime import datetime
        from app.services.companion_service import save_config

        bridge.bind(self.user.id, SESSION)
        save_config(self.user, {"enabled": True, "channels": ["astrbot"]})
        with user_scope(self.user.id):
            record = notify("主动分享", "过一会再发", ["astrbot"], user_id=self.user.id,
                            delivery_context={"kind": "companion"})[0]
        row = AstrBotOutbox.query.one()
        row.available_at = datetime(2026, 10, 19, 0)
        row.expires_at = datetime(2026, 10, 21, 0)
        db.session.commit()
        self.assertEqual(bridge.claim(self.user.id, now=datetime(2026, 10, 19, 16)), [])  # local midnight
        self.assertEqual(AstrBotOutbox.query.one().attempts, 0)
        self.assertEqual(len(bridge.claim(self.user.id, now=datetime(2026, 10, 20, 1))), 1)
        save_config(self.user, {"enabled": False})
        self.assertEqual(bridge.claim(self.user.id, now=datetime(2026, 10, 20, 2)), [])
        db.session.refresh(record)
        self.assertEqual(record.status, "failed")
        self.assertEqual(AstrBotOutbox.query.one().status, "cancelled")

    def test_missing_image_support_and_oversized_text_fail_visibly(self):
        bridge.bind(self.user.id, SESSION)
        with user_scope(self.user.id):
            image_record = notify("image", "body", ["astrbot"], image_bytes=b"x", user_id=self.user.id)[0]
            text_record = notify("long", "x" * 12001, ["astrbot"], user_id=self.user.id)[0]
        self.assertEqual(image_record.status, "failed")
        self.assertEqual(text_record.status, "failed")
        self.assertEqual(AstrBotOutbox.query.count(), 0)

    def test_deleted_notification_does_not_break_delivery_ack(self):
        bridge.bind(self.user.id, SESSION)
        record = self.queue()
        db.session.delete(record)
        db.session.commit()
        message = bridge.claim(self.user.id)[0]
        self.assertEqual(bridge.acknowledge(self.user.id, message["id"], message["lease_token"], "sent")["status"], "sent")
