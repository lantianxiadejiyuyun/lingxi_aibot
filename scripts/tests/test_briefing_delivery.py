"""Briefing delivery regression tests: isolated SQLite and no real notifications."""
from unittest.mock import Mock, PropertyMock, patch

from scripts.tests.support import IsolatedAppTestCase

from app.ai import briefing
from app.extensions import db
from app.models.notification import Notification, STATUS_FAILED, STATUS_SENT
from app.models.scheduled_job import ScheduledJob
from app.services import notify_service
from app.services.settings_service import get_own_setting, set_setting
from app.utils.scoping import clear_current_user_id, set_current_user_id


KINDS = {
    "morning": ("morning_briefing", briefing.morning, "早安简报"),
    "noon": ("noon_briefing", briefing.noon, "午间简报"),
    "evening": ("evening_review", briefing.evening, "晚间复盘"),
}
EXTERNAL_CHANNELS = ("feishu_app", "feishu", "serverchan")


class BriefingDeliveryTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.user = self.make_user()
        self.client = self.client_for(self.user)
        # Exercise real Markdown generation and notification persistence while
        # replacing only the external transports and the optional LLM.
        llm_patch = patch("app.ai.llm.LLMClient", return_value=Mock(is_configured=False))
        llm_patch.start()
        self.addCleanup(llm_patch.stop)
        self.senders = {}
        for name in EXTERNAL_CHANNELS:
            cls = notify_service.CHANNELS[name]
            configured_patch = patch.object(cls, "configured", new_callable=PropertyMock,
                                            return_value=True)
            configured_patch.start()
            self.addCleanup(configured_patch.stop)
            send_patch = patch.object(cls, "send")
            self.senders[name] = send_patch.start()
            self.addCleanup(send_patch.stop)

    def make_job(self, kind, *, user=None, enabled=True, params=None, cron="0 7 * * *"):
        owner = user or self.user
        action, _, title = KINDS[kind]
        job = ScheduledJob(
            user_id=owner.id, job_key=action, name=title, action=action,
            cron=cron, enabled=enabled, is_builtin=True,
            params=dict(params or {}),
        )
        db.session.add(job)
        db.session.commit()
        return job

    def own_job(self, kind, user=None):
        return ScheduledJob.query.filter_by(
            user_id=(user or self.user).id, job_key=KINDS[kind][0], is_builtin=True,
        ).populate_existing().one()

    def form(self, **changes):
        fields = {
            "briefing_delivery_settings": "1",
            "briefing_time_morning": "07:15",
            "briefing_time_noon": "12:30",
            "briefing_time_evening": "21:45",
            "briefing_enabled_morning": "1",
            "briefing_enabled_noon": "1",
            "briefing_enabled_evening": "1",
        }
        fields.update(changes)
        return fields

    def records(self):
        return Notification.query.order_by(Notification.id).all()

    def run_action(self, kind, params=None):
        set_current_user_id(self.user.id)
        try:
            KINDS[kind][1](self.user, params)
        finally:
            clear_current_user_id()

    def test_all_briefings_deliver_to_every_selected_platform(self):
        for kind, (_, _, title) in KINDS.items():
            with self.subTest(kind=kind):
                previous_count = Notification.query.count()
                self.run_action(kind, {"channels": list(EXTERNAL_CHANNELS)})
                records = self.records()[previous_count:]
                self.assertEqual([row.channel for row in records], list(EXTERNAL_CHANNELS))
                for row in records:
                    self.assertEqual(row.user_id, self.user.id)
                    self.assertEqual(row.status, STATUS_SENT)
                    self.assertIsNotNone(row.sent_at)
                    self.assertIn(title, row.title)
                    self.assertIn("## ", row.body)
                    sent = self.senders[row.channel].call_args
                    self.assertEqual(sent.args, (row.title, row.body))

    def test_notification_groups_expand_and_deduplicate_for_all_briefings(self):
        set_setting("notify_groups", {"工作": ["feishu_app", "serverchan"]}, user_id=self.user.id)
        for kind in KINDS:
            with self.subTest(kind=kind):
                previous_count = Notification.query.count()
                self.run_action(kind, {"channels": ["group:工作", "feishu_app", "inapp"]})
                records = self.records()[previous_count:]
                self.assertEqual([row.channel for row in records], ["feishu_app", "serverchan", "inapp"])
                self.assertTrue(all(row.status == STATUS_SENT for row in records))
        self.assertEqual(self.senders["feishu_app"].call_count, 3)
        self.assertEqual(self.senders["serverchan"].call_count, 3)

    def test_scene_then_account_default_are_inherited_for_all_briefings(self):
        set_setting("default_channels", ["serverchan"], user_id=self.user.id)
        for scene, expected in ((["feishu_app"], "feishu_app"), ([], "serverchan")):
            set_setting("notify_briefing_channels", scene, user_id=self.user.id)
            for kind in KINDS:
                with self.subTest(kind=kind, scene=scene):
                    previous_count = Notification.query.count()
                    self.run_action(kind)
                    rows = self.records()[previous_count:]
                    self.assertEqual([row.channel for row in rows], [expected])
                    self.assertEqual(rows[0].status, STATUS_SENT)

    def test_legacy_channel_override_still_delivers_to_selected_platform(self):
        set_setting("notify_briefing_channels", ["serverchan"], user_id=self.user.id)
        for kind in KINDS:
            with self.subTest(kind=kind):
                self.run_action(kind, {"channel": "feishu_app"})
                self.assertEqual(self.records()[-1].channel, "feishu_app")

    def test_failed_delivery_persists_results_and_marks_each_action_failed(self):
        self.senders["feishu_app"].side_effect = RuntimeError("offline transport failed")
        for kind in KINDS:
            with self.subTest(kind=kind):
                previous_count = Notification.query.count()
                with self.assertRaisesRegex(RuntimeError, "通知中心"):
                    self.run_action(kind, {"channels": ["feishu_app", "inapp"]})
                rows = self.records()[previous_count:]
                self.assertEqual([row.status for row in rows], [STATUS_FAILED, STATUS_SENT])
                self.assertIn("offline transport failed", rows[0].error)
                self.assertIsNone(rows[0].sent_at)
                self.assertIsNotNone(rows[1].sent_at)

    def test_unconfigured_platform_is_a_delivery_failure(self):
        with patch.object(notify_service.CHANNELS["feishu_app"], "configured",
                          new_callable=PropertyMock, return_value=False):
            with self.assertRaisesRegex(RuntimeError, "通知中心"):
                self.run_action("noon", {"channels": ["feishu_app"]})
        self.senders["feishu_app"].assert_not_called()
        self.assertEqual(self.records()[0].status, STATUS_FAILED)
        self.assertIn("未配置", self.records()[0].error)

    def test_settings_create_three_user_jobs_and_save_independent_delivery(self):
        other = self.make_user("other-briefing-user", is_admin=False)
        other_jobs = {kind: self.make_job(kind, user=other, enabled=False,
                                        params={"channels": ["inapp"]}) for kind in KINDS}
        set_setting("notify_groups", {"工作": ["feishu_app", "serverchan"]}, user_id=self.user.id)
        fields = self.form(
            briefing_channels_morning=["feishu_app", "serverchan"],
            briefing_channels_noon=["group:工作"],
            briefing_channels_evening=["feishu"],
        )
        fields.pop("briefing_enabled_noon")
        response = self.client.post("/settings/briefing", data=fields)
        self.assertEqual(response.status_code, 302)
        self.assertIn("tab=briefing", response.location)
        expected = {
            "morning": ("15 7 * * *", True, ["feishu_app", "serverchan"]),
            "noon": ("30 12 * * *", False, ["group:工作"]),
            "evening": ("45 21 * * *", True, ["feishu"]),
        }
        for kind, (cron, enabled, channels) in expected.items():
            with self.subTest(kind=kind):
                job = self.own_job(kind)
                self.assertEqual((job.action, job.cron, job.enabled), (KINDS[kind][0], cron, enabled))
                self.assertEqual(job.params["channels"], channels)
                self.assertEqual(get_own_setting(f"briefing_time_{kind}", user_id=self.user.id),
                                 fields[f"briefing_time_{kind}"])
                other_job = other_jobs[kind]
                db.session.refresh(other_job)
                self.assertEqual((other_job.cron, other_job.enabled, other_job.params),
                                 ("0 7 * * *", False, {"channels": ["inapp"]}))
                self.assertIsNone(get_own_setting(f"briefing_time_{kind}", user_id=other.id))

    def test_empty_selection_removes_legacy_override_and_inherits_scene(self):
        for kind in KINDS:
            self.make_job(kind, params={"channel": "inapp", "channels": ["inapp"],
                                        "unrelated": "preserved"})
        set_setting("notify_briefing_channels", ["feishu_app"], user_id=self.user.id)
        response = self.client.post("/settings/briefing", data=self.form())
        self.assertEqual(response.status_code, 302)
        for kind in KINDS:
            with self.subTest(kind=kind):
                job = self.own_job(kind)
                self.assertEqual(job.params, {"unrelated": "preserved"})
                self.run_action(kind, job.params)
                self.assertEqual(self.records()[-1].channel, "feishu_app")

    def test_legacy_time_only_form_preserves_delivery_and_enabled(self):
        for kind in KINDS:
            self.make_job(kind, enabled=False, params={"channel": "feishu_app", "marker": kind})
        fields = {key: value for key, value in self.form().items() if key.startswith("briefing_time_")}
        response = self.client.post("/settings/briefing", data=fields)
        self.assertEqual(response.status_code, 302)
        for kind in KINDS:
            with self.subTest(kind=kind):
                job = self.own_job(kind)
                self.assertFalse(job.enabled)
                self.assertEqual(job.params, {"channel": "feishu_app", "marker": kind})
        self.assertEqual(self.own_job("noon").cron, "30 12 * * *")

    def test_invalid_times_channels_and_foreign_groups_do_not_partially_save(self):
        other = self.make_user("group-owner", is_admin=False)
        set_setting("notify_groups", {"private": ["feishu_app"]}, user_id=other.id)
        for kind in KINDS:
            self.make_job(kind, enabled=False, params={"channels": ["inapp"]})
            set_setting(f"briefing_time_{kind}", "06:00", user_id=self.user.id)
        invalid_forms = (
            {"briefing_time_noon": "24:30"},
            {"briefing_time_evening": "12:60"},
            {"briefing_channels_evening": ["missing-platform"]},
            {"briefing_channels_evening": ["group:missing"]},
            {"briefing_channels_evening": ["group:private"]},
        )
        for invalid in invalid_forms:
            with self.subTest(invalid=invalid):
                response = self.client.post("/settings/briefing", data=self.form(**invalid))
                self.assertEqual(response.status_code, 302)
                for kind in KINDS:
                    self.assertEqual(get_own_setting(f"briefing_time_{kind}", user_id=self.user.id), "06:00")
                    job = self.own_job(kind)
                    self.assertEqual((job.cron, job.enabled, job.params),
                                     ("0 7 * * *", False, {"channels": ["inapp"]}))

    def test_settings_page_offers_delivery_and_send_controls_for_each_briefing(self):
        for kind in KINDS:
            self.make_job(kind, params={"channels": ["feishu_app"]})
        response = self.client.get("/settings/?tab=briefing")
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="briefing_delivery_settings"', response.text)
        for kind, (_, _, title) in KINDS.items():
            with self.subTest(kind=kind):
                self.assertIn(title, response.text)
                self.assertIn(f'name="briefing_time_{kind}"', response.text)
                self.assertIn(f'name="briefing_enabled_{kind}"', response.text)
                self.assertIn(f'name="briefing_channels_{kind}"', response.text)
                self.assertIn(f'/settings/briefing/send/{kind}', response.text)

    def test_manual_send_uses_saved_channels_even_when_job_and_scheduler_disabled(self):
        self.app.scheduler = None
        for kind in KINDS:
            self.make_job(kind, enabled=False, params={"channels": ["feishu_app"]})
        for kind in KINDS:
            with self.subTest(kind=kind):
                response = self.client.post(f"/settings/briefing/send/{kind}",
                                            data={"briefing_channels_" + kind: "serverchan"})
                self.assertEqual(response.status_code, 302)
                self.assertIn("tab=briefing", response.location)
                job = self.own_job(kind)
                self.assertFalse(job.enabled)
                self.assertIsNotNone(job.last_run_utc)
                self.assertIn("成功", job.last_status)
                self.assertEqual(self.records()[-1].channel, "feishu_app")
                self.assertEqual(self.records()[-1].user_id, self.user.id)
        self.assertEqual(self.senders["feishu_app"].call_count, 3)
        self.senders["serverchan"].assert_not_called()

    def test_manual_send_failure_is_visible_and_never_reported_successful(self):
        self.app.scheduler = None
        self.make_job("noon", params={"channels": ["feishu_app", "inapp"]})
        self.senders["feishu_app"].side_effect = RuntimeError("offline delivery failure")
        response = self.client.post("/settings/briefing/send/noon")
        self.assertEqual(response.status_code, 302)
        self.assertIn("tab=briefing", response.location)
        job = self.own_job("noon")
        self.assertIsNotNone(job.last_run_utc)
        self.assertIn("失败", job.last_status)
        self.assertNotIn("成功", job.last_status)
        self.assertIn("通知中心", job.last_status)
        self.assertEqual([row.status for row in self.records()], [STATUS_FAILED, STATUS_SENT])
        with self.client.session_transaction() as session:
            flashes = list(session.get("_flashes", []))
        self.assertTrue(any(category == "error" and "通知中心" in message for category, message in flashes))
        self.assertFalse(any(category == "success" for category, _ in flashes))

    def test_manual_send_uses_current_users_job_and_settings(self):
        other = self.make_user("other-recipient", is_admin=False)
        other_job = self.make_job("evening", user=other, params={"channels": ["serverchan"]})
        self.make_job("evening")
        set_setting("notify_briefing_channels", ["feishu_app"], user_id=self.user.id)
        set_setting("notify_briefing_channels", ["serverchan"], user_id=other.id)
        response = self.client.post("/settings/briefing/send/evening")
        self.assertEqual(response.status_code, 302)
        row = self.records()[0]
        self.assertEqual((row.user_id, row.channel, row.status), (self.user.id, "feishu_app", STATUS_SENT))
        db.session.refresh(other_job)
        self.assertIsNone(other_job.last_run_utc)
        self.assertEqual(other_job.last_status, "")
        self.senders["serverchan"].assert_not_called()

    def test_manual_send_requires_login_and_rejects_unknown_kind(self):
        unauthenticated = self.app.test_client()
        response = unauthenticated.post("/settings/briefing/send/noon")
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.location)
        response = self.client.post("/settings/briefing/send/unknown")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Notification.query.count(), 0)


if __name__ == "__main__":
    import unittest

    unittest.main()
