"""Daily briefings survive chat cleanup and appear once per local calendar day."""
from datetime import date, datetime, timedelta, timezone
import gzip
import json
from unittest.mock import Mock, patch

from flask_login import login_user

from scripts.tests.support import IsolatedAppTestCase
from app.ai import briefing
from app.extensions import db
from app.models.conversation import Conversation, Message
from app.models.daily_report import DailyReport
from app.models.event import Event
from app.services import calendar_service, daily_report_service
from app.utils.scoping import user_scope
from app.utils.timeutil import get_tz


REPORT_DATE = date(2026, 10, 10)
TITLES = {
    "morning": "☀️ 早安简报",
    "noon": "🕛 午间简报",
    "evening": "🌙 晚间复盘",
}
MORNING_GUIDE = (
    "早上好！今日安排已整理好（见上方简报）。"
    "需要我帮你排优先级、调整日程或补充提醒吗？"
)


class FrozenDatetime(datetime):
    instant = datetime(2026, 10, 10, 16, 30, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.instant.replace(tzinfo=None)
        return cls.instant.astimezone(tz)


class DailyReportTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.user = self.make_user()
        self.client = self.client_for(self.user)
        llm_patch = patch("app.ai.llm.LLMClient", return_value=Mock(is_configured=False))
        llm_patch.start()
        self.addCleanup(llm_patch.stop)

    def record(self, kind, content, *, user=None, day=REPORT_DATE,
               conversation_id=None, generated_at=None, missing_only=False):
        daily_report_service.record_briefing(
            user or self.user, kind, day, content, conversation_id,
            generated_at=generated_at or datetime(2026, 10, 10, 1), missing_only=missing_only,
        )
        db.session.commit()
        return DailyReport.query.filter_by(
            user_id=(user or self.user).id, report_date=day,
        ).populate_existing().one()

    def old_conversation(self, kind, messages, *, user=None, day=REPORT_DATE,
                         created_at=None):
        conv = Conversation(
            user_id=(user or self.user).id,
            title=f"{TITLES[kind]} {day.isoformat()}",
            created_at=created_at or datetime(2026, 10, 10, 1),
            updated_at=created_at or datetime(2026, 10, 10, 1),
        )
        for index, (role, content) in enumerate(messages):
            conv.messages.append(Message(
                role=role, content=content,
                created_at=(created_at or datetime(2026, 10, 10, 1)) + timedelta(minutes=index),
            ))
        db.session.add(conv)
        db.session.commit()
        return conv

    def test_three_sections_and_regeneration_keep_one_report_per_day(self):
        for kind in TITLES:
            self.record(kind, f"{kind} original")
        before = DailyReport.query.one()
        report_id = before.id
        regenerated_at = datetime(2026, 10, 10, 6, 25)
        report = self.record("noon", "updated noon", generated_at=regenerated_at)
        self.assertEqual(DailyReport.query.count(), 1)
        self.assertEqual(report.id, report_id)
        self.assertEqual(report.morning_content, "morning original")
        self.assertEqual(report.noon_content, "updated noon")
        self.assertEqual(report.evening_content, "evening original")
        self.assertEqual(report.noon_generated_at, regenerated_at)

    def test_delayed_older_generation_cannot_replace_a_newer_section(self):
        newest = datetime(2026, 10, 10, 6, 25)
        self.record("noon", "latest completed content", generated_at=newest)
        self.record("noon", "delayed older content", generated_at=newest - timedelta(minutes=1))
        report = DailyReport.query.one()
        self.assertEqual(report.noon_content, "latest completed content")
        self.assertEqual(report.noon_generated_at, newest)

    def test_listing_is_user_scoped_inclusive_and_chronological(self):
        other = self.make_user("other-reporter", is_admin=False)
        for offset in (1, -1, 0):
            self.record("morning", f"owner {offset}", day=REPORT_DATE + timedelta(days=offset))
        self.record("morning", "private other report", user=other)
        rows = daily_report_service.list_reports(
            self.user, REPORT_DATE, REPORT_DATE + timedelta(days=1), backfill=False,
        )
        self.assertEqual([row.report_date for row in rows], [REPORT_DATE, date(2026, 10, 11)])
        self.assertTrue(all(row.user_id == self.user.id for row in rows))
        self.assertEqual([row.morning_content for row in rows], ["owner 0", "owner 1"])

    def test_record_does_not_commit_callers_transaction(self):
        daily_report_service.record_briefing(
            self.user, "morning", REPORT_DATE, "must roll back", None,
        )
        db.session.rollback()
        self.assertEqual(DailyReport.query.count(), 0)

    def test_backfill_uses_last_report_before_first_user_message_not_guide_or_followup(self):
        conv = self.old_conversation("morning", [
            ("assistant", "first generated version"),
            ("assistant", MORNING_GUIDE),
            ("assistant", "latest generated version"),
            ("assistant", MORNING_GUIDE),
            ("user", "Can you change my afternoon schedule?"),
            ("assistant", "private follow-up answer, not a daily report"),
        ])
        self.old_conversation("noon", [("assistant", "legacy noon content")])
        self.old_conversation("evening", [("assistant", "legacy evening content")])
        daily_report_service.backfill_reports(self.user, REPORT_DATE, REPORT_DATE)
        report = DailyReport.query.one()
        self.assertEqual(report.morning_content, "latest generated version")
        self.assertEqual(report.morning_conversation_id, conv.id)
        self.assertEqual(report.noon_content, "legacy noon content")
        self.assertEqual(report.evening_content, "legacy evening content")

    def test_backfill_is_idempotent_and_never_overwrites_a_new_section(self):
        self.old_conversation("morning", [("assistant", "old morning")])
        self.old_conversation("noon", [("assistant", "old noon")])
        other = self.make_user("legacy-other", is_admin=False)
        self.old_conversation("evening", [("assistant", "other evening")], user=other)
        newest = datetime(2026, 10, 10, 6)
        self.record("morning", "new morning", generated_at=newest)
        for _ in range(2):
            daily_report_service.backfill_reports(self.user, REPORT_DATE, REPORT_DATE)
        report = DailyReport.query.one()
        self.assertEqual(report.morning_content, "new morning")
        self.assertEqual(report.morning_generated_at, newest)
        self.assertEqual(report.noon_content, "old noon")
        self.assertFalse(report.evening_content)

    def test_backfill_does_not_treat_a_conversation_reply_as_a_generated_report(self):
        self.old_conversation("morning", [
            ("assistant", MORNING_GUIDE),
            ("user", "Only a follow-up remains"),
            ("assistant", "this is a reply"),
        ])
        daily_report_service.backfill_reports(self.user, REPORT_DATE, REPORT_DATE)
        self.assertEqual(DailyReport.query.count(), 0)

    def test_backfill_ignores_whitespace_assistant_messages(self):
        self.old_conversation("morning", [
            ("assistant", "valid historical report"),
            ("assistant", " \n\t "),
        ])
        self.old_conversation("noon", [("assistant", " \n ")])
        daily_report_service.backfill_reports(self.user, REPORT_DATE, REPORT_DATE)
        report = DailyReport.query.one()
        self.assertEqual(report.morning_content, "valid historical report")
        self.assertFalse(report.noon_content)

    def test_sync_cli_imports_only_selected_users_valid_months_once_without_delivery(self):
        from app.models.notification import Notification
        other = self.make_user("unsynced-history-owner", is_admin=False)
        self.old_conversation("morning", [("assistant", "December report")], day=date(2025, 12, 31))
        self.old_conversation("noon", [("assistant", "February noon")], day=date(2026, 2, 1))
        self.old_conversation("evening", [("assistant", "February evening")], day=date(2026, 2, 1))
        self.old_conversation("morning", [("assistant", "other private report")],
                              user=other, day=date(2026, 5, 14))
        for title in (
            "☀️ 早安简报 2026-01-99", "☀️ 早安简报 20260101",
            "🕛 午间简报 2026-01-01 extra", "Ordinary chat 2026-01-01",
        ):
            conv = Conversation(user_id=self.user.id, title=title)
            conv.messages.append(Message(role="assistant", content="not a generated report"))
            db.session.add(conv)
        db.session.commit()
        message_count = Message.query.count()
        runner = self.app.test_cli_runner()
        with patch.object(daily_report_service, "backfill_reports",
                          wraps=daily_report_service.backfill_reports) as backfill, \
                patch("app.services.notify_service.notify_for") as notify, \
                patch("app.ai.llm.LLMClient") as llm:
            result = runner.invoke(args=["sync-daily-reports", "--user-id", str(self.user.id)])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("补入 3 段历史简报", result.output)
            self.assertEqual([call.args[1:] for call in backfill.call_args_list], [
                (date(2025, 12, 1), date(2025, 12, 31)),
                (date(2026, 2, 1), date(2026, 2, 28)),
            ])
            rows = DailyReport.query.order_by(DailyReport.report_date).all()
            self.assertEqual([row.report_date for row in rows], [date(2025, 12, 31), date(2026, 2, 1)])
            self.assertTrue(all(row.user_id == self.user.id for row in rows))
            self.assertEqual(rows[0].morning_content, "December report")
            self.assertEqual(rows[1].noon_content, "February noon")
            self.assertEqual(rows[1].evening_content, "February evening")
            original_ids = [row.id for row in rows]
            result = runner.invoke(args=["sync-daily-reports", "--user-id", str(self.user.id)])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("补入 0 段历史简报", result.output)
            self.assertEqual([row.id for row in DailyReport.query.order_by(DailyReport.report_date)],
                             original_ids)
            notify.assert_not_called()
            llm.assert_not_called()
        self.assertEqual(DailyReport.query.filter_by(user_id=other.id).count(), 0)
        self.assertEqual(Message.query.count(), message_count)
        self.assertEqual(Notification.query.count(), 0)

    def test_existing_daily_content_survives_clear_all_conversations(self):
        conv = self.old_conversation("morning", [("assistant", "persistent report")])
        self.record("morning", "persistent report", conversation_id=conv.id)
        response = self.client.post("/chat/api/clear-all")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Conversation.query.filter_by(user_id=self.user.id).count(), 0)
        self.assertEqual(Message.query.count(), 0)
        rows = daily_report_service.list_reports(self.user, REPORT_DATE, REPORT_DATE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].morning_content, "persistent report")

    def test_calendar_month_and_day_show_one_report_without_creating_schedule_events(self):
        for kind in TITLES:
            self.record(kind, f"{kind} calendar body")
        other = self.make_user("calendar-other", is_admin=False)
        self.record("morning", "foreign calendar body", user=other)
        response = self.client.get("/calendar/api/events?year=2026&month=10")
        self.assertEqual(response.status_code, 200)
        items = response.get_json()["data"]["events"]
        reports = [item for item in items if item.get("kind") == "daily_report"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["date"], "2026-10-10")
        self.assertTrue(str(reports[0]["id"]).startswith("daily-report-"))
        from app.blueprints.calendar import _day_occurrences
        with self.app.test_request_context("/"):
            login_user(self.user)
            day_items = _day_occurrences("2026-10-10", get_tz(self.user.timezone))
        self.assertEqual([item["id"] for item in day_items], [reports[0]["id"]])
        self.assertEqual(Event.query.count(), 0)
        self.assertEqual(calendar_service.list_events(
            datetime(2026, 10, 1), datetime(2026, 11, 1), self.user.id,
        ), [])

    def test_calendar_request_backfills_legacy_reports_only_for_current_user(self):
        self.old_conversation("noon", [("assistant", "my historical noon")])
        other = self.make_user("unseen-legacy", is_admin=False)
        self.old_conversation("noon", [("assistant", "foreign historical noon")], user=other)
        response = self.client.get("/calendar/api/events?year=2026&month=10")
        self.assertEqual(response.status_code, 200)
        reports = [item for item in response.get_json()["data"]["events"]
                   if item.get("kind") == "daily_report"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(DailyReport.query.one().noon_content, "my historical noon")

    def test_initial_calendar_page_renders_daily_sections_as_readonly_escaped_content(self):
        conv = self.old_conversation("morning", [("assistant", "historical content")])
        self.record("morning", '<script>alert("report")</script>', conversation_id=conv.id)
        self.record("noon", "visible noon content")
        response = self.client.get("/calendar/?year=2026&month=10&day=2026-10-10")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        day_html = html.split('id="day-list"', 1)[1].split("</section>", 1)[0]
        self.assertEqual(day_html.count('data-kind="daily_report"'), 1)
        self.assertIn("早安简报", day_html)
        self.assertIn("午间简报", day_html)
        self.assertIn("visible noon content", day_html)
        self.assertIn("&lt;script&gt;", day_html)
        self.assertNotIn('<script>alert("report")</script>', html)
        self.assertNotIn('data-mode="edit"', day_html)
        self.assertIn(f'href="/chat/?conversation={conv.id}"', day_html)

    def test_calendar_conversation_links_only_point_to_existing_owned_chats(self):
        own = self.old_conversation("morning", [("assistant", "own conversation")])
        other = self.make_user("foreign-chat-owner", is_admin=False)
        foreign = self.old_conversation("noon", [("assistant", "private conversation")], user=other)
        self.record("morning", "morning archive", conversation_id=own.id)
        self.record("noon", "noon archive", conversation_id=foreign.id)
        self.record("evening", "evening archive", conversation_id=999999)
        response = self.client.get("/calendar/api/events?year=2026&month=10")
        row = response.get_json()["data"]["events"][0]
        urls = {section["kind"]: section["conversation_url"] for section in row["sections"]}
        self.assertEqual(urls["morning"], f"/chat/?conversation={own.id}")
        self.assertFalse(urls["noon"])
        self.assertFalse(urls["evening"])
        self.assertEqual(self.client.post(f"/chat/api/delete/{own.id}").status_code, 200)
        response = self.client.get("/calendar/api/events?year=2026&month=10")
        sections = response.get_json()["data"]["events"][0]["sections"]
        self.assertEqual(sections[0]["content"], "morning archive")
        self.assertFalse(sections[0]["conversation_url"])

    def test_linked_old_report_conversation_is_listed_beyond_recent_twenty_with_owner_check(self):
        old = self.old_conversation("morning", [("assistant", "old daily report")],
                                    created_at=datetime(2026, 1, 1))
        other = self.make_user("old-chat-other", is_admin=False)
        foreign = self.old_conversation("morning", [("assistant", "foreign old chat")], user=other)
        for offset in range(25):
            db.session.add(Conversation(
                user_id=self.user.id, title=f"Recent chat {offset}",
                created_at=datetime(2026, 10, 10, offset % 24),
                updated_at=datetime(2026, 10, 10, offset % 24),
            ))
        db.session.commit()
        default = self.client.get("/chat/api/conversations").get_json()["data"]
        self.assertEqual(len(default), 20)
        self.assertNotIn(old.id, [row["id"] for row in default])
        response = self.client.get(f"/chat/api/conversations?conversation={old.id}")
        self.assertEqual(response.status_code, 200)
        ids = [row["id"] for row in response.get_json()["data"]]
        self.assertIn(old.id, ids)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn(foreign.id, ids)
        response = self.client.get(f"/chat/api/conversations?conversation={foreign.id}")
        self.assertIn(response.status_code, (200, 404))
        if response.status_code == 200:
            self.assertNotIn(foreign.id, [row["id"] for row in response.get_json()["data"]])
        self.assertNotIn("foreign old chat", response.get_data(as_text=True))

    def test_build_briefing_groups_all_three_fallbacks_on_users_local_date(self):
        with patch.object(briefing, "datetime", FrozenDatetime), user_scope(self.user.id):
            contents = {kind: briefing.build_briefing(kind, self.user) for kind in TITLES}
        report = DailyReport.query.one()
        self.assertEqual(report.report_date, date(2026, 10, 11))
        self.assertEqual(report.timezone, "Asia/Shanghai")
        for kind, content in contents.items():
            with self.subTest(kind=kind):
                self.assertIn("## ", content)
                self.assertEqual(getattr(report, f"{kind}_content"), content)
                conversation = db.session.get(Conversation, getattr(report, f"{kind}_conversation_id"))
                self.assertEqual(conversation.user_id, self.user.id)
                self.assertEqual(conversation.messages[-1].content, content)

    def test_same_instant_uses_each_users_timezone(self):
        other = self.make_user("pacific-user", is_admin=False)
        other.timezone = "America/Los_Angeles"
        db.session.commit()
        with patch.object(briefing, "datetime", FrozenDatetime):
            for user in (self.user, other):
                with user_scope(user.id):
                    briefing.build_briefing("morning", user)
        mine = DailyReport.query.filter_by(user_id=self.user.id).one()
        theirs = DailyReport.query.filter_by(user_id=other.id).one()
        self.assertEqual(mine.report_date, date(2026, 10, 11))
        self.assertEqual(theirs.report_date, date(2026, 10, 10))
        self.assertEqual(theirs.timezone, "America/Los_Angeles")

    def test_report_save_failure_rolls_back_new_conversation_and_message(self):
        with patch("app.services.daily_report_service.record_briefing",
                   side_effect=RuntimeError("database failure")), user_scope(self.user.id):
            with self.assertRaisesRegex(RuntimeError, "database failure"):
                briefing.build_briefing("noon", self.user)
        db.session.rollback()
        self.assertEqual(Conversation.query.count(), 0)
        self.assertEqual(Message.query.count(), 0)
        self.assertEqual(DailyReport.query.count(), 0)

    def test_llm_fallback_and_push_failure_still_leave_the_report_available(self):
        from app.ai.llm import LLMError
        llm = Mock(is_configured=True)
        llm.chat.side_effect = LLMError("upstream unavailable")
        with patch("app.ai.llm.LLMClient", return_value=llm), \
                patch("app.ai.prompts.build_system_prompt", return_value="isolated prompt"), \
                patch("app.services.notify_service.notify_for",
                      return_value=[Mock(status="failed")]), user_scope(self.user.id):
            with self.assertRaisesRegex(RuntimeError, "通知中心"):
                briefing.noon(self.user, {"channels": ["feishu_app"]})
        report = DailyReport.query.one()
        self.assertIn("## 今日剩余日程", report.noon_content)
        conversation = db.session.get(Conversation, report.noon_conversation_id)
        self.assertEqual(conversation.messages[-1].content, report.noon_content)

    def test_token_daily_report_endpoint_uses_token_owner_not_browser_session(self):
        from app.utils.api_auth import assign_user_api_token
        other = self.make_user("token-report-owner", is_admin=False)
        my_token = assign_user_api_token(self.user)
        other_token = assign_user_api_token(other)
        self.record("morning", "my daily archive")
        self.record("morning", "other daily archive", user=other)
        url = "/api/v1/daily-reports?date_from=2026-10-10&date_to=2026-10-10"
        self.assertEqual(self.app.test_client().get(url).status_code, 401)
        self.assertEqual(self.client.get(url).status_code, 401)
        for token, expected, unexpected in (
            (my_token, "my daily archive", "other daily archive"),
            (other_token, "other daily archive", "my daily archive"),
        ):
            with self.subTest(expected=expected):
                response = self.client.get(url, headers={"Authorization": "Bearer " + token})
                self.assertEqual(response.status_code, 200)
                rows = response.get_json()["data"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["kind"], "daily_report")
                self.assertEqual(rows[0]["date"], "2026-10-10")
                self.assertEqual(rows[0]["sections"][0]["content"], expected)
                self.assertNotIn(unexpected, response.get_data(as_text=True))

    def test_token_daily_report_endpoint_validates_ranges_and_includes_both_endpoints(self):
        from app.utils.api_auth import assign_user_api_token
        headers = {"Authorization": "Bearer " + assign_user_api_token(self.user)}
        self.record("morning", "first day")
        self.record("morning", "last day", day=REPORT_DATE + timedelta(days=1))
        url = "/api/v1/daily-reports"
        response = self.client.get(url, query_string={
            "date_from": "2026-10-10", "date_to": "2026-10-11",
        }, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["date"] for row in response.get_json()["data"]],
                         ["2026-10-10", "2026-10-11"])
        for query in (
            {}, {"date_from": "2026-10-10"},
            {"date_from": "2026-10-11", "date_to": "2026-10-10"},
            {"date_from": "2026-02-30", "date_to": "2026-10-10"},
            {"date_from": "2025-10-10", "date_to": "2026-10-11"},
            {"date_from": "20261010", "date_to": "20261011"},
            {"date_from": "2026-10-10", "date_to": "2026-10-11", "user_id": "999"},
        ):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(url, query_string=query,
                                                 headers=headers).status_code, 400)
        response = self.client.get(url, query_string={
            "date_from": "2025-10-10", "date_to": "2026-10-10",
        }, headers=headers)
        self.assertEqual(response.status_code, 200)

    def test_backup_restore_preserves_report_date_and_generation_timestamps(self):
        from app.services.backup_service import backup_to_json, restore_from_json
        timestamp = datetime(2026, 10, 10, 1, 23, 45, 678901)
        report = self.record("morning", "backup morning", generated_at=timestamp)
        self.record("evening", "backup evening", generated_at=timestamp + timedelta(hours=12))
        report_id = report.id
        path = backup_to_json()
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            exported = json.load(stream)["tables"]["daily_reports"]
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["report_date"], "2026-10-10")
        DailyReport.query.delete(synchronize_session=False)
        db.session.commit()
        self.assertEqual(DailyReport.query.count(), 0)
        restored = restore_from_json(path)
        db.session.expire_all()
        report = DailyReport.query.one()
        self.assertEqual(restored["daily_reports"], 1)
        self.assertEqual(report.id, report_id)
        self.assertEqual(report.report_date, REPORT_DATE)
        self.assertEqual(report.morning_generated_at, timestamp)
        self.assertEqual(report.evening_generated_at, timestamp + timedelta(hours=12))
        self.assertEqual(report.morning_content, "backup morning")
        self.assertEqual(report.evening_content, "backup evening")
        self.assertFalse(report.noon_content)
        self.assertIsInstance(report.created_at, datetime)
