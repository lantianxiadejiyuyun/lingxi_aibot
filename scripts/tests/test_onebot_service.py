"""Direct QQ account isolation and replay safety, without live QQ or model calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import re
import secrets
import threading
import time
from unittest.mock import patch

from scripts.tests.support import IsolatedAppTestCase
from app.extensions import db
from app.models.conversation import Conversation
from app.models.onebot import OneBotBinding, OneBotBindingCode, OneBotEvent
from app.models.notification import Notification
from app.services import onebot_service as bridge
from app.services.channels.onebot import OneBotChannel
from app.services.notify_service import notify
from app.utils.integration_api import ApiError
from app.utils.scoping import current_user_id, user_scope
from app.utils.timeutil import utcnow


class OneBotServiceTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.other = self.make_user("qq_other", is_admin=False)
        sender = patch("app.services.onebot_service._send")
        self.send = sender.start()
        self.addCleanup(sender.stop)
        runner = patch("app.services.onebot_service.iter_chat_events", return_value=iter([
            {"type": "start", "data": {}}, {"type": "done", "data": "你好，已收到"},
        ]))
        self.chat = runner.start()
        self.addCleanup(runner.stop)
        self.sequence = 0

    def event(self, message="你好", **updates):
        self.sequence += 1
        value = {"post_type": "message", "message_type": "private", "self_id": 12345,
                 "user_id": 67890, "message_id": self.sequence, "time": time.time(),
                 "message": [{"type": "text", "data": {"text": message}}]}
        value.update(updates)
        return value

    def bind(self, user=None, peer="67890", bot="12345"):
        code = bridge.create_binding_code((user or self.user).id)["code"]
        return bridge._consume_code(bot, peer, code)

    def test_binding_code_is_hashed_expires_and_rotation_invalidates_old_code(self):
        first = bridge.create_binding_code(self.user.id)
        row = OneBotBindingCode.query.one()
        self.assertNotEqual(row.code_hash, first["code"])
        self.assertEqual(len(row.code_hash), 64)
        self.assertIsInstance(first["expires_at"], str)
        second = bridge.create_binding_code(self.user.id)
        self.assertNotEqual(first["code"], second["code"])
        with self.assertRaises(ApiError):
            bridge._consume_code("12345", "67890", first["code"])
        OneBotBindingCode.query.one().expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
        with self.assertRaises(ApiError):
            bridge._consume_code("12345", "67890", second["code"])
        self.assertFalse(bridge.binding_view(self.user.id)["bound"])

    def test_code_is_one_time_and_creates_owned_conversation(self):
        code = bridge.create_binding_code(self.user.id)["code"]
        binding = bridge._consume_code("12345", "67890", code.lower())
        self.assertEqual(OneBotBindingCode.query.count(), 0)
        self.assertEqual(Conversation.query.one().user_id, self.user.id)
        self.assertEqual(binding["conversation_id"], Conversation.query.one().id)
        with self.assertRaises(ApiError):
            bridge._consume_code("12345", "99999", code)
        self.assertEqual(bridge.binding_view(self.user.id)["qq_user_id"], "67890")

    def test_one_qq_cannot_bind_two_accounts_and_code_not_consumed_on_conflict(self):
        self.bind()
        code = bridge.create_binding_code(self.other.id)["code"]
        with self.assertRaises(ApiError) as raised:
            bridge._consume_code("12345", "67890", code)
        self.assertEqual(raised.exception.status, 409)
        self.assertFalse(bridge.binding_view(self.other.id)["bound"])
        self.assertEqual(OneBotBindingCode.query.count(), 1)
        bridge._consume_code("12345", "99999", code)
        self.assertEqual(OneBotBinding.query.count(), 2)

    def test_account_rebind_rotates_generation_and_preserves_old_history(self):
        first = self.bind()
        same = self.bind()
        self.assertEqual(first["conversation_id"], same["conversation_id"])
        self.assertNotEqual(first["generation"], same["generation"])
        changed = self.bind(peer="99999")
        self.assertNotEqual(changed["conversation_id"], first["conversation_id"])
        self.assertEqual(Conversation.query.count(), 2)
        with self.assertRaises(ValueError):
            bridge.send_to_binding(first, "旧消息")
        self.send.assert_not_called()

    def test_unbind_revokes_code_and_refuses_stale_generation(self):
        first = self.bind()
        current = self.bind(peer="99999")
        with self.assertRaises(ApiError):
            bridge.unbind(self.user.id, expected_generation=first["generation"])
        self.assertEqual(bridge.binding_view(self.user.id)["qq_user_id"], "99999")
        bridge.create_binding_code(self.user.id)
        bridge.unbind(self.user.id, expected_generation=current["generation"])
        self.assertFalse(bridge.binding_view(self.user.id)["bound"])
        self.assertEqual(OneBotBindingCode.query.count(), 0)
        self.assertEqual(Conversation.query.count(), 2)

    def test_binding_does_not_enable_companion_or_change_preferences(self):
        prefs = dict(self.user.prefs)
        self.bind()
        db.session.refresh(self.user)
        self.assertEqual(self.user.prefs, prefs)

    def test_identity_binding_status_and_unbind_commands(self):
        bridge.handle_event(self.event("/lingxi 身份"))
        self.assertIn("67890", self.send.call_args.args[2])
        code = bridge.create_binding_code(self.user.id)["code"]
        bridge.handle_event(self.event("/lingxi 绑定 " + code))
        self.assertTrue(bridge.binding_view(self.user.id)["bound"])
        bridge.handle_event(self.event("/lingxi 状态"))
        self.assertIn("已绑定", self.send.call_args.args[2])
        bridge.handle_event(self.event("/lingxi 解绑"))
        self.assertFalse(bridge.binding_view(self.user.id)["bound"])
        self.chat.assert_not_called()

    def test_unknown_sender_is_not_allowed_to_run_chat(self):
        self.bind()
        bridge.handle_event(self.event(user_id=99999))
        self.send.assert_not_called()
        self.chat.assert_not_called()

    def test_group_and_self_and_message_sent_events_are_ignored(self):
        self.bind()
        for updates in ({"message_type": "group"}, {"group_id": 3333},
                        {"user_id": 12345}, {"post_type": "message_sent"}):
            bridge.handle_event(self.event(**updates))
        self.assertEqual(OneBotEvent.query.count(), 0)
        self.chat.assert_not_called()
        self.send.assert_not_called()

    def test_cq_and_non_text_cannot_trigger_commands_or_ai(self):
        self.bind()
        messages = ["[CQ:image,file=x]/lingxi 解绑", "[cq:at,qq=12345]/lingxi 解绑",
                    [{"type": "image", "data": {"file": "x"}},
                     {"type": "text", "data": {"text": "/lingxi 解绑"}}],
                    [{"type": "text", "data": {"text": "[CQ:json,data=x]"}}]]
        for message in messages:
            bridge.handle_event(self.event(message=message))
        self.assertTrue(bridge.binding_view(self.user.id)["bound"])
        self.assertEqual(self.send.call_count, len(messages))
        self.chat.assert_not_called()

    def test_normal_chat_and_slash_commands_reuse_owned_integration_service(self):
        binding = self.bind()
        bridge.handle_event(self.event("/help"))
        args, kwargs = self.chat.call_args
        self.assertEqual(args, (binding["conversation_id"], self.user.id, "/help"))
        self.assertEqual(kwargs["request_id"], "onebot:12345:1")
        self.send.assert_called_once_with("12345", "67890", "你好，已收到", image_bytes=None)
        self.assertEqual(current_user_id(), 0)

    def test_duplicate_delivery_never_runs_tools_twice(self):
        self.bind()
        event = self.event("创建一个待办")
        bridge.handle_event(event)
        bridge.handle_event(event)
        self.chat.assert_called_once()
        self.send.assert_called_once()
        self.assertEqual(OneBotEvent.query.one().status, "done")

    def test_failed_send_is_recorded_and_duplicate_does_not_rerun_ai(self):
        self.bind()
        self.send.side_effect = TimeoutError("simulated timeout")
        event = self.event()
        bridge.handle_event(event)
        bridge.handle_event(event)
        self.chat.assert_called_once()
        self.assertEqual(OneBotEvent.query.one().status, "failed")

    def test_rebinding_during_generation_drops_old_reply(self):
        self.bind()

        def events(*args, **kwargs):
            bridge.unbind(self.user.id)
            self.bind(self.other)
            yield {"type": "done", "data": "原账号的隐私"}

        self.chat.side_effect = events
        bridge.handle_event(self.event())
        self.send.assert_not_called()
        self.assertTrue(bridge.binding_view(self.other.id)["bound"])
        self.assertEqual(OneBotEvent.query.one().status, "failed")

    def test_error_events_are_visible_without_retry(self):
        self.bind()
        self.chat.return_value = iter([{"type": "error", "data": "请配置模型"},
                                       {"type": "done", "data": ""}])
        bridge.handle_event(self.event())
        self.assertEqual(self.send.call_args.args[2], "请配置模型")

    def test_generator_exception_uses_safe_error(self):
        self.bind()

        def events(*args, **kwargs):
            yield {"type": "start", "data": {}}
            raise RuntimeError("secret exception")

        self.chat.side_effect = events
        bridge.handle_event(self.event())
        self.assertEqual(self.send.call_args.args[2], bridge.CHAT_FAILURE)

    def test_invalid_and_old_events_do_not_claim_or_run(self):
        for updates in ({"time": None}, {"time": True}, {"time": float("inf")},
                        {"time": time.time() - 8 * 86400}, {"time": time.time() + 600},
                        {"self_id": True}, {"user_id": "not-a-number"}, {"message_id": "bad"}):
            bridge.handle_event(self.event(**updates))
        self.assertEqual(OneBotEvent.query.count(), 0)

    def test_old_dedupe_rows_are_cleaned_but_old_events_still_rejected(self):
        db.session.add(OneBotEvent(self_id="12345", message_id="98765", status="done",
                                  created_at=utcnow() - timedelta(days=8)))
        db.session.commit()
        bridge.handle_event(self.event("/lingxi 身份"))
        self.assertIsNone(db.session.get(OneBotEvent, ("12345", "98765")))
        bridge.handle_event(self.event(message_id=98765, time=time.time() - 8 * 86400))
        self.assertIsNone(db.session.get(OneBotEvent, ("12345", "98765")))

    def test_negative_message_ids_are_supported_and_numeric_routes_normalized(self):
        self.bind()
        bridge.handle_event(self.event(message_id=-112233, self_id="012345", user_id="067890"))
        self.assertIsNotNone(db.session.get(OneBotEvent, ("12345", "-112233")))
        self.chat.assert_called_once()

    def test_notification_channel_is_scoped_and_preserves_pending_transaction(self):
        self.bind()
        with user_scope(self.user.id):
            self.assertTrue(OneBotChannel().configured)
            records = notify("提醒", "今天的待办", ["onebot"], user_id=self.user.id)
        self.assertEqual(records[0].status, "sent")
        self.assertEqual(Notification.query.count(), 1)
        self.send.assert_called_once_with("12345", "67890", "提醒\n\n今天的待办", image_bytes=None)
        with user_scope(self.other.id):
            self.assertFalse(OneBotChannel().configured)
            records = notify("别人的通知", "不允许发送", ["onebot"], user_id=self.other.id)
        self.assertEqual(records[0].status, "failed")
        self.assertEqual(self.send.call_count, 1)

    def test_notification_owner_mismatch_is_rejected(self):
        self.bind()
        with user_scope(self.user.id):
            records = notify("其他账号", "隔离", ["onebot"], user_id=self.other.id)
        self.assertEqual(records[0].status, "failed")
        self.send.assert_not_called()

    def test_image_bytes_are_forwarded_and_failed_transport_is_not_marked_sent(self):
        self.bind()
        with user_scope(self.user.id):
            records = notify("图片", "正文", ["onebot"], image_bytes=b"image", user_id=self.user.id)
            self.assertEqual(self.send.call_args.kwargs["image_bytes"], b"image")
            self.assertEqual(records[0].status, "sent")
            self.send.side_effect = TimeoutError("QQ 超时")
            failed = notify("失败", "正文", ["onebot"], user_id=self.user.id)[0]
        self.assertEqual(failed.status, "failed")

    def test_notification_delivery_does_not_commit_callers_changes(self):
        self.bind()
        pending = Notification(user_id=self.user.id, title="待提交", body="内容", channel="onebot")
        db.session.add(pending)
        db.session.flush()
        pending_id = pending.id
        with user_scope(self.user.id):
            OneBotChannel().deliver(pending)
        db.session.rollback()
        self.assertIsNone(db.session.get(Notification, pending_id))

    def test_binding_view_is_account_scoped(self):
        self.bind()
        self.assertTrue(bridge.binding_view(self.user.id)["bound"])
        self.assertEqual(bridge.binding_view(self.other.id), {
            "bound": False, "self_id": "", "qq_user_id": "", "conversation_id": None,
        })

    def test_missing_account_cannot_create_code(self):
        for value in (0, None, True, 999999):
            with self.assertRaises(ApiError):
                bridge.create_binding_code(value)
        self.assertEqual(OneBotBindingCode.query.count(), 0)

    def test_concurrent_consumers_cannot_reuse_one_code(self):
        code = bridge.create_binding_code(self.user.id)["code"]
        start = threading.Barrier(2)

        def consume(peer):
            with self.app.app_context():
                start.wait(timeout=5)
                try:
                    bridge._consume_code("12345", peer, code)
                    return "bound"
                except ApiError:
                    return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(consume, ("67890", "99999")))
        self.assertCountEqual(results, ["bound", "rejected"])
        self.assertEqual(OneBotBinding.query.count(), 1)
        self.assertEqual(Conversation.query.count(), 1)

    def test_concurrent_accounts_cannot_claim_the_same_qq(self):
        codes = [bridge.create_binding_code(user.id)["code"] for user in (self.user, self.other)]
        start = threading.Barrier(2)

        def consume(code):
            with self.app.app_context():
                start.wait(timeout=5)
                try:
                    bridge._consume_code("12345", "67890", code)
                    return "bound"
                except ApiError:
                    return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(consume, codes))
        self.assertCountEqual(results, ["bound", "rejected"])
        self.assertEqual(OneBotBinding.query.count(), 1)
        self.assertEqual(OneBotBindingCode.query.count(), 1)


def mysql_concurrency_probe(app):
    """Run with a caller-created disposable MySQL DB; no network messages.

    The caller owns database creation/removal and supplies a TESTING app. Its DB
    name MUST be lingxi_onebot_probe_<16 lowercase hex>. Returns passed check names.
    This callable allows the deployment runner to pass credentials only in memory.
    """
    from app.models.user import User
    from sqlalchemy import text

    if not app.testing:
        raise RuntimeError("MySQL probe requires TESTING=True")
    with app.app_context():
        if db.engine.dialect.name != "mysql" or not re.fullmatch(
                r"lingxi_onebot_probe_[0-9a-f]{16}", str(db.engine.url.database)):
            raise RuntimeError("Refusing non-temporary MySQL database")
        db.create_all()
        users = [User(username="onebot_probe_" + secrets.token_hex(6), password_hash="probe-only") for _ in range(2)]
        db.session.add_all(users)
        db.session.commit()
        ids = [user.id for user in users]
        db.session.rollback()
    checks = []

    def parallel_bind(codes, peers):
        barrier = threading.Barrier(2)

        def one(index):
            with app.app_context():
                barrier.wait(timeout=10)
                try:
                    return bridge._consume_code("12345", peers[index], codes[index])
                except ApiError:
                    return None
                finally:
                    db.session.remove()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(one, (0, 1)))

    with app.app_context():
        code = bridge.create_binding_code(ids[0])["code"]
    result = parallel_bind([code, code], ["67890", "99999"])
    assert sum(bool(item) for item in result) == 1, "one-time code was reused"
    checks.append("one_time_code_concurrency")

    with app.app_context():
        bridge.unbind(ids[0])
        codes = [bridge.create_binding_code(uid)["code"] for uid in ids]
    result = parallel_bind(codes, ["67890", "67890"])
    assert sum(bool(item) for item in result) == 1, "QQ route has multiple owners"
    checks.append("cross_account_route_uniqueness")

    # A previously opened repeatable-read transaction must not restore a code
    # invalidated by another account session while it was waiting for locks.
    with app.app_context():
        for uid in ids:
            bridge.unbind(uid)
        old = bridge.create_binding_code(ids[0])["code"]
        OneBotBindingCode.query.filter_by(user_id=ids[0]).first()

        def rotate():
            with app.app_context():
                return bridge.create_binding_code(ids[0])["code"]

        with ThreadPoolExecutor(max_workers=1) as pool:
            fresh = pool.submit(rotate).result(timeout=20)
        try:
            bridge._consume_code("12345", "67890", old)
        except ApiError:
            pass
        else:
            raise AssertionError("stale repeatable-read code was accepted")
        binding = bridge._consume_code("12345", "67890", fresh)
        checks.append("repeatable_read_code_rotation")

        # An independent connection holding the named guard must exclude even
        # this process's guard. No production credentials or messages are logged.
        suffix = bridge.hashlib.sha256(str(db.engine.url.database).encode()).hexdigest()[:20]
        lock_name = "lingxi:onebot:" + suffix
        with bridge._route_guard():
            with db.engine.connect() as connection:
                assert connection.execute(text("SELECT GET_LOCK(:name, 0)"), {"name": lock_name}).scalar() == 0
                connection.commit()
        checks.append("cross_process_advisory_guard")

        with patch("app.services.onebot_service._send") as sender:
            bridge.unbind(ids[0])
            other_code = bridge.create_binding_code(ids[1])["code"]
            bridge._consume_code("12345", "67890", other_code)
            try:
                bridge.send_to_binding(binding, "must never leave process")
            except ValueError:
                pass
            else:
                raise AssertionError("stale reply was sent")
            sender.assert_not_called()
        checks.append("stale_generation_delivery_blocked")

        with patch("app.services.onebot_service._send"), patch(
                "app.services.onebot_service.iter_chat_events", return_value=iter([
                    {"type": "done", "data": "simulated"}])) as executor:
            event = {"post_type": "message", "message_type": "private", "self_id": 12345,
                     "user_id": 67890, "message_id": 77, "time": time.time(), "message": "simulated"}
            bridge.handle_event(event)
            bridge.handle_event(event)
            assert executor.call_count == 1, "duplicate event repeated tools"
        checks.append("durable_event_deduplication")
    return checks
