"""Companion checks run on an isolated DB; no real model or notification network."""
from datetime import datetime, timedelta
import json
from unittest.mock import Mock, patch

from scripts.tests.support import IsolatedAppTestCase
from app.extensions import db
from app.models.companion import CompanionActivity, CompanionState
from app.models.conversation import Conversation, Message
from app.models.event import Event
from app.models.memory import Memory
from app.models.notification import Notification
from app.models.scheduled_job import ScheduledJob
from app.models.setting import Setting
from app.models.task import Task
from app.services import companion_service as service, companion_sources
from app.utils.scoping import current_user_id, user_scope


NOW = datetime(2026, 10, 19, 2)  # 10:00 Asia/Shanghai


class CompanionTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.llm = Mock(is_configured=True)
        self.llm._read_config.return_value = {"timeout": 600}
        self.reply()
        p = patch("app.ai.llm.LLMClient", return_value=self.llm)
        p.start()
        self.addCleanup(p.stop)
        self.discovery = patch("app.services.companion_sources.discover", return_value=[]).start()
        self.addCleanup(patch.stopall)

    def reply(self, message="今天那个小目标，可以先做最轻松的一步。", decision="send", ids=None):
        self.llm.chat.return_value = (json.dumps({"decision": decision, "message": message,
                                                "source_ids": ids or []}, ensure_ascii=False), [])

    def enable(self, **values):
        return service.save_config(self.user, {"enabled": True, **values})

    def user_message(self, user=None, at=NOW, content="最近想学吉他"):
        conv = Conversation(user_id=(user or self.user).id, title="chat")
        conv.messages.append(Message(role="user", content=content, created_at=at))
        db.session.add(conv)
        db.session.commit()
        return conv

    def test_default_off_own_settings_only_and_tick_is_silent(self):
        db.session.add(Setting(user_id=0, key=service.CONFIG_KEY, value={"enabled": True}))
        db.session.commit()
        self.assertFalse(service.load_config(self.user)["enabled"])
        self.assertEqual(service.run_tick(self.user, now=NOW), {"status": "skip", "reason": "disabled"})
        self.llm.chat.assert_not_called()
        self.discovery.assert_not_called()
        self.assertEqual(Notification.query.count(), 0)

    def test_save_is_scoped_validated_and_has_one_job(self):
        other = self.make_user("other", is_admin=False)
        self.enable(interests="吉他,动漫\n吉他")
        service.save_config(self.user, {"daily_limit": "2"})
        self.assertEqual(service.load_config(self.user)["interests"], ["吉他", "动漫"])
        self.assertFalse(service.load_config(other)["enabled"])
        jobs = ScheduledJob.query.filter_by(user_id=self.user.id, action="companion_tick").all()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].cron, "interval:15")
        self.assertTrue(jobs[0].enabled)
        service.save_config(self.user, {"enabled": False})
        self.assertFalse(jobs[0].enabled)
        for values in ({"daily_limit": 0}, {"enabled": "maybe"}, {"active_start": "25:00"},
                       {"channels": ["unknown"]}, {"interests": ["x" * 41]},
                       {"min_interval_minutes": 1}, {"chat_cooldown_minutes": 1},
                       {"inject_key": True}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                service.save_config(self.user, values)

    def test_quiet_hours_cross_midnight_and_active_hours(self):
        self.enable(active_start="00:00", active_end="00:00", quiet_start="22:00", quiet_end="08:00")
        for instant in (datetime(2026, 10, 18, 14), datetime(2026, 10, 18, 19), datetime(2026, 10, 18, 23, 59)):
            self.assertEqual(service.run_tick(self.user, now=instant)["reason"], "quiet_hours")
        service.save_config(self.user, {"active_start": "09:00", "active_end": "17:00"})
        self.assertEqual(service.run_tick(self.user, now=datetime(2026, 10, 19, 10))["reason"], "outside_active_hours")
        self.llm.chat.assert_not_called()

    def test_cross_midnight_active_hours_and_equal_quiet_disabled(self):
        self.enable(active_start="20:00", active_end="03:00", quiet_start="00:00", quiet_end="00:00")
        self.assertEqual(service.run_tick(self.user, now=datetime(2026, 10, 18, 17))["status"], "sent")

    def test_chat_cooldown_is_user_scoped(self):
        self.enable()
        self.user_message(at=NOW - timedelta(minutes=10))
        self.assertEqual(service.run_tick(self.user, now=NOW)["reason"], "chat_cooldown")
        self.llm.chat.assert_not_called()
        other = self.make_user("other", is_admin=False)
        service.save_config(other, {"enabled": True})
        self.assertEqual(service.run_tick(other, now=NOW)["status"], "sent")

    def test_success_is_archived_as_assistant_and_honors_interval(self):
        self.enable()
        result = service.run_tick(self.user, now=NOW)
        self.assertEqual(result["status"], "sent")
        activity = db.session.get(CompanionActivity, result["activity_id"])
        conv = db.session.get(Conversation, activity.conversation_id)
        self.assertEqual(conv.user_id, self.user.id)
        self.assertEqual([message.role for message in conv.messages], ["assistant"])
        self.assertEqual(Notification.query.one().status, "sent")
        self.assertEqual(service.run_tick(self.user, now=NOW + timedelta(minutes=30))["reason"], "min_interval")
        self.llm.chat.assert_called_once()
        self.assertIsNone(self.llm.chat.call_args.kwargs["tools"])
        self.assertIsNone(db.session.get(CompanionState, self.user.id).lease_until)
        self.assertEqual(self.llm._read_config()["timeout"], 60)

    def test_daily_limit_is_local_date_and_counts_failure_attempts(self):
        self.enable(daily_limit=1)
        with patch.object(service.notify_service, "notify", side_effect=RuntimeError("offline")):
            self.assertEqual(service.run_tick(self.user, now=NOW)["status"], "failed")
        self.assertEqual(service.run_tick(self.user, now=NOW + timedelta(hours=3))["reason"], "daily_limit")
        self.reply("明天的新想法。")
        self.assertEqual(service.run_tick(self.user, now=NOW + timedelta(days=1))["status"], "sent")

    def test_persistent_exact_deduplication_does_not_replay_next_day(self):
        self.enable()
        service.run_tick(self.user, now=NOW)
        self.assertEqual(service.run_tick(self.user, now=NOW + timedelta(days=1))["reason"], "duplicate")
        self.assertEqual(Notification.query.count(), 1)

    def test_live_lease_prevents_concurrent_work_stale_lease_recovers(self):
        self.enable()
        state = db.session.get(CompanionState, self.user.id)
        state.lease_token, state.lease_until = "another-worker", NOW + timedelta(minutes=5)
        db.session.commit()
        self.assertEqual(service.run_tick(self.user, now=NOW)["reason"], "busy")
        self.llm.chat.assert_not_called()
        state.lease_until = NOW - timedelta(seconds=1)
        db.session.commit()
        self.assertEqual(service.run_tick(self.user, now=NOW)["status"], "sent")

    def test_lost_lease_never_sends_or_clears_new_owner(self):
        self.enable()
        def steal(*args, **kwargs):
            state = db.session.get(CompanionState, self.user.id)
            state.lease_token, state.lease_until = "new-owner", NOW + timedelta(minutes=5)
            db.session.commit()
            return {"decision": "send", "message": "old worker", "source_ids": []}
        with patch.object(service, "_decide", side_effect=steal):
            self.assertEqual(service.run_tick(self.user, now=NOW)["reason"], "lease_expired")
        self.assertEqual(Notification.query.count(), 0)
        self.assertEqual(db.session.get(CompanionState, self.user.id).lease_token, "new-owner")

    def test_disable_or_new_chat_during_generation_suppresses_delivery(self):
        self.enable()
        def disable(*args, **kwargs):
            service.save_config(self.user, {"enabled": False})
            return {"decision": "send", "message": "不应发送", "source_ids": []}
        with patch.object(service, "_decide", side_effect=disable):
            self.assertEqual(service.run_tick(self.user, now=NOW)["reason"], "disabled")
        self.enable()
        state = db.session.get(CompanionState, self.user.id)
        state.last_attempt_at = None
        db.session.commit()
        def chat(*args, **kwargs):
            self.user_message(at=NOW)
            return {"decision": "send", "message": "不应发送", "source_ids": []}
        with patch.object(service, "_decide", side_effect=chat):
            self.assertEqual(service.run_tick(self.user, now=NOW)["reason"], "chat_cooldown")
        self.assertEqual(Notification.query.count(), 0)

    def test_skip_and_llm_failure_never_send_and_throttle_evaluation(self):
        self.enable()
        self.reply("", decision="skip")
        self.assertEqual(service.run_tick(self.user, now=NOW)["reason"], "nothing_to_share")
        self.assertEqual(service.run_tick(self.user, now=NOW + timedelta(minutes=15))["reason"], "decision_cooldown")
        self.llm.chat.side_effect = RuntimeError("provider unavailable")
        self.assertEqual(service.run_tick(self.user, now=NOW + timedelta(minutes=30))["reason"], "model_error")
        self.assertEqual(Notification.query.count(), 0)
        self.assertEqual(CompanionActivity.query.count(), 2)
        self.assertTrue(all(not activity.body for activity in CompanionActivity.query.all()))

    def test_unconfigured_model_is_quiet(self):
        self.enable()
        self.llm.is_configured = False
        self.assertEqual(service.run_tick(self.user, now=NOW)["reason"], "model_unconfigured")
        self.llm.chat.assert_not_called()

    def test_invalid_json_tool_calls_and_untrusted_urls_are_rejected(self):
        self.enable()
        invalid = [
            ("not json", []),
            (json.dumps({"decision": "send", "message": "hi", "source_ids": [], "thought": "private reasoning"}), []),
            (json.dumps({"decision": "send", "message": "hi", "source_ids": ["invented"]}), []),
            (json.dumps({"decision": "send", "message": "visit https://evil.example", "source_ids": []}), []),
            (json.dumps({"decision": "send", "message": "hi", "source_ids": []}), [{"name": "delete_task"}]),
        ]
        for index, reply in enumerate(invalid):
            self.llm.chat.return_value = reply
            result = service.run_tick(self.user, now=NOW + timedelta(minutes=30 * index))
            self.assertEqual(result["status"], "failed")
        self.assertEqual(Notification.query.count(), 0)
        self.assertTrue(all("thought" not in activity.body for activity in CompanionActivity.query.all()))

    def test_delivery_intent_is_committed_before_send_and_failure_not_success(self):
        self.enable()
        def fail(*args, **kwargs):
            row = CompanionActivity.query.one()
            self.assertEqual(row.status, "dispatching")
            db.session.rollback()  # Committed intent survives transport rollback.
            self.assertEqual(CompanionActivity.query.count(), 1)
            raise RuntimeError("transport unavailable")
        with patch.object(service.notify_service, "notify", side_effect=fail):
            self.assertEqual(service.run_tick(self.user, now=NOW)["status"], "failed")
        self.assertIsNone(db.session.get(CompanionState, self.user.id).last_sent_at)
        self.assertEqual(Conversation.query.count(), 0)
        self.assertEqual(service.run_tick(self.user, now=NOW + timedelta(days=1))["reason"], "duplicate")

    def test_pending_transport_is_accepted_and_ack_updates_view(self):
        self.enable()
        def pending(title, body, channels, user_id, delivery_context=None):
            self.assertEqual(delivery_context, {"kind": "companion"})
            record = Notification(user_id=user_id, channel="astrbot", title=title, body=body, status="pending")
            db.session.add(record)
            db.session.commit()
            return [record]
        with patch.object(service.notify_service, "notify", side_effect=pending):
            self.assertEqual(service.run_tick(self.user, now=NOW)["status"], "queued")
        self.assertIsNotNone(db.session.get(CompanionState, self.user.id).last_sent_at)
        self.assertEqual(service.run_tick(self.user, now=NOW + timedelta(minutes=30))["reason"], "min_interval")
        notification = Notification.query.one()
        notification.status = "sent"
        db.session.commit()
        self.assertEqual(service.get_view(self.user)["activities"][0]["status"], "sent")
        notification.status = "failed"
        db.session.commit()
        self.assertEqual(service.get_view(self.user)["activities"][0]["status"], "failed")

    def test_context_and_activity_are_private_and_thread_scope_restored(self):
        self.enable()
        other = self.make_user("other", is_admin=False)
        self.user_message(at=NOW - timedelta(days=1), content="我的吉他兴趣")
        self.user_message(user=other, at=NOW - timedelta(days=1), content="OTHER_SECRET")
        db.session.add_all([Memory(user_id=self.user.id, content="喜欢吉他"),
                            Memory(user_id=other.id, content="OTHER_SECRET"),
                            Task(user_id=self.user.id, title="练琴"), Task(user_id=other.id, title="OTHER_SECRET")])
        db.session.commit()
        with user_scope(other.id):
            service.run_tick(self.user, now=NOW)
            self.assertEqual(current_user_id(), other.id)
        prompt = self.llm.chat.call_args.args[0][-1]["content"]
        self.assertIn("我的吉他兴趣", prompt)
        self.assertIn("练琴", prompt)
        self.assertNotIn("OTHER_SECRET", prompt)
        self.assertEqual(service.get_view(other)["activities"], [])

    def test_bili_source_dedup_and_auditable_link_not_model_invented(self):
        self.enable(share_bilibili=True, interests=["吉他"])
        source = {"id": "bili:real", "kind": "bilibili_search", "title": "真实视频",
                  "url": "https://www.bilibili.com/video/BV1xx411c7mD", "snippet": "这是搜索摘要"}
        self.discovery.return_value = [source]
        self.reply("搜到一个练习视频，简介看起来适合入门。", ids=["bili:real"])
        self.assertEqual(service.run_tick(self.user, now=NOW)["status"], "sent")
        self.assertIn(source["url"], Notification.query.one().body)
        self.assertEqual(CompanionActivity.query.one().sources, [source])
        self.reply("换一种话术再次推荐视频。", ids=["bili:real"])
        self.assertEqual(service.run_tick(self.user, now=NOW + timedelta(days=1))["reason"], "duplicate")
        self.assertEqual(Notification.query.count(), 1)

    def test_unanswered_messages_reduce_frequency(self):
        self.enable(min_interval_minutes=30, daily_limit=6)
        service.run_tick(self.user, now=NOW)
        self.reply("第二条不同内容")
        service.run_tick(self.user, now=NOW + timedelta(minutes=30))
        self.reply("第三条不同内容")
        self.assertEqual(service.run_tick(self.user, now=NOW + timedelta(hours=1))["reason"], "awaiting_reply")
        self.assertEqual(Notification.query.count(), 2)

    def test_search_failure_can_still_choose_silence_without_network_retry(self):
        self.enable(share_bilibili=True, interests=["吉他"])
        self.discovery.side_effect = RuntimeError("search down")
        self.reply("", decision="skip")
        self.assertEqual(service.run_tick(self.user, now=NOW)["status"], "skip")
        self.discovery.assert_called_once()
        self.assertEqual(Notification.query.count(), 0)

    def test_crashed_dispatch_reserves_interval_without_marking_success(self):
        self.enable()
        service._log(self.user.id, NOW, "dispatching", "sharing", body="transport outcome unknown")
        self.assertEqual(service.run_tick(self.user, now=NOW + timedelta(minutes=30))["reason"], "min_interval")
        self.assertIsNone(db.session.get(CompanionState, self.user.id).last_sent_at)
        self.llm.chat.assert_not_called()

    def test_continuous_conversation_is_private_and_recreated_after_deletion(self):
        self.enable()
        first = service.run_tick(self.user, now=NOW)
        conv_id = db.session.get(CompanionActivity, first["activity_id"]).conversation_id
        self.reply("第二天不同的分享")
        second = service.run_tick(self.user, now=NOW + timedelta(days=1))
        self.assertEqual(db.session.get(CompanionActivity, second["activity_id"]).conversation_id, conv_id)
        conv = db.session.get(Conversation, conv_id)
        self.assertEqual(conv.title, "灵犀日常")
        self.assertEqual(len(conv.messages), 2)
        db.session.delete(conv)
        db.session.commit()
        self.reply("删除后新的分享")
        third = service.run_tick(self.user, now=NOW + timedelta(days=2))
        self.assertEqual(third["status"], "sent")
        self.assertEqual(Conversation.query.count(), 1)
        self.assertEqual(Conversation.query.one().messages[0].content, "删除后新的分享")

    def test_calendar_busy_including_recurring_and_future_evidence_are_scoped(self):
        self.enable()
        other = self.make_user("other-calendar", is_admin=False)
        db.session.add_all([
            Event(user_id=self.user.id, title="每日会议", start_utc=NOW - timedelta(days=3, minutes=10),
                  end_utc=NOW - timedelta(days=3) + timedelta(minutes=20), rrule="FREQ=DAILY"),
            Event(user_id=other.id, title="OTHER_PRIVATE_EVENT", start_utc=NOW, end_utc=NOW + timedelta(hours=4)),
            Event(user_id=self.user.id, title="之后上课", start_utc=NOW + timedelta(hours=2),
                  end_utc=NOW + timedelta(hours=3)),
        ])
        db.session.commit()
        self.assertEqual(service.run_tick(self.user, now=NOW)["reason"], "calendar_busy")
        self.llm.chat.assert_not_called()
        self.assertEqual(service.run_tick(self.user, now=NOW + timedelta(minutes=30))["status"], "sent")
        prompt = self.llm.chat.call_args.args[0][-1]["content"]
        self.assertIn("之后上课", prompt)
        self.assertNotIn("OTHER_PRIVATE_EVENT", prompt)
        self.assertTrue(any(item["kind"] == "context_event" for item in CompanionActivity.query.one().sources))

    def test_delivery_check_ignores_consumed_budget_but_rechecks_current_settings(self):
        self.enable(daily_limit=1)
        self.assertEqual(service.run_tick(self.user, now=NOW)["status"], "sent")
        self.assertEqual(service.delivery_block_reason(self.user, now=NOW), "")
        self.assertEqual(service.delivery_block_reason(self.user, now=NOW + timedelta(hours=13)), "quiet_hours")
        service.save_config(self.user, {"enabled": False})
        self.assertEqual(service.delivery_block_reason(self.user, now=NOW), "disabled")
        self.assertEqual(Notification.query.count(), 1)

    def test_event_without_end_is_busy_for_thirty_minutes_after_start(self):
        self.enable()
        db.session.add(Event(user_id=self.user.id, title="未填结束时间的会议",
                             start_utc=NOW - timedelta(minutes=10), end_utc=None))
        db.session.commit()
        self.assertEqual(service.run_tick(self.user, now=NOW)["reason"], "calendar_busy")
        self.assertEqual(service.delivery_block_reason(self.user, now=NOW), "calendar_busy")
        self.assertEqual(service.delivery_block_reason(self.user, now=NOW + timedelta(minutes=20)), "")
        self.llm.chat.assert_not_called()
        self.assertEqual(Notification.query.count(), 0)

    def test_daily_evaluation_budget_includes_interrupted_and_duplicate_decisions(self):
        self.enable()
        for index in range(24):
            service._log(self.user.id, NOW - timedelta(minutes=index), "evaluating", "preparing")
        self.assertEqual(service.run_tick(self.user, now=NOW)["reason"], "decision_limit")
        self.llm.chat.assert_not_called()

    def test_model_evaluation_is_committed_before_request_and_duplicate_is_logged(self):
        self.enable()
        def verify(*args, **kwargs):
            row = CompanionActivity.query.one()
            self.assertEqual(row.status, "evaluating")
            db.session.rollback()
            self.assertEqual(CompanionActivity.query.count(), 1)
            return {"decision": "send", "message": "固定内容", "source_ids": []}
        with patch.object(service, "_decide", side_effect=verify):
            self.assertEqual(service.run_tick(self.user, now=NOW)["status"], "sent")
        self.reply("固定内容")
        self.assertEqual(service.run_tick(self.user, now=NOW + timedelta(days=1))["reason"], "duplicate")
        self.assertEqual(CompanionActivity.query.count(), 2)
        self.assertEqual(CompanionActivity.query.order_by(CompanionActivity.id.desc()).first().reason, "duplicate")


class CompanionSourcesTests(IsolatedAppTestCase):
    def test_public_search_is_bounded_and_rejects_non_video_or_host_confusion(self):
        source = {"title": "<b>吉他</b>", "url": "https://www.bilibili.com/video/BV1xx411c7mD/?from=search",
                  "snippet": "<script>do something</script> intro"}
        with patch.object(companion_sources.web_search_service, "search", return_value=[
                source, {**source, "url": "https://bilibili.com.evil.example/video/BV1xx411c7mD"},
                {**source, "url": "http://127.0.0.1/video/BV1xx411c7mD"}]) as search:
            results = companion_sources.discover({"share_bilibili": True, "interests": ["吉他"]}, NOW)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(results[0]["title"], "吉他")
        self.assertEqual(search.call_args.kwargs["limit"], 3)
        self.assertTrue(search.call_args.args[0].startswith("site:bilibili.com/video/ "))

    def test_discovery_is_opt_in_and_never_fetches_arbitrary_pages(self):
        with patch.object(companion_sources.web_search_service, "search") as search:
            self.assertEqual(companion_sources.discover({"share_bilibili": False, "interests": ["x"]}, NOW), [])
            self.assertEqual(companion_sources.discover({"share_bilibili": True, "interests": []}, NOW), [])
        search.assert_not_called()
        for url in ("javascript:alert(1)", "https://www.bilibili.com@evil.example/video/BV1xx411c7mD",
                    "https://www.bilibili.com/", "https://www.bilibili.com:444/video/BV1xx411c7mD"):
            self.assertIsNone(companion_sources.canonical_bilibili_url(url))
