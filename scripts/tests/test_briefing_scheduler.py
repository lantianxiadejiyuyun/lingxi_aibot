"""隔离回归：简报默认渠道、任务页多渠道保留与调度用户作用域。"""
from unittest.mock import patch

from werkzeug.datastructures import MultiDict

from scripts.tests.support import IsolatedAppTestCase

from app.extensions import db
from app.models.scheduled_job import ScheduledJob
from app.scheduler import ACTIONS, SchedulerService, _job_func
from app.services.install_service import seed_builtin_jobs_for_user
from app.services.settings_service import get_setting, set_setting
from app.utils import scoping


class BriefingSchedulerTests(IsolatedAppTestCase):
    def _job(self, user, *, action="noon_briefing", params=None):
        job = ScheduledJob(
            user_id=user.id, job_key=action, name="午间简报", action=action,
            cron="0 12 * * *", enabled=True, is_builtin=True, params=params or {},
        )
        db.session.add(job)
        db.session.commit()
        return job

    def test_new_briefings_inherit_scene_and_existing_selection_is_retained(self):
        user = self.make_user()
        seed_builtin_jobs_for_user(user)
        actions = {"morning_briefing", "noon_briefing", "evening_review"}
        jobs = ScheduledJob.query.filter(ScheduledJob.action.in_(actions)).all()
        self.assertEqual(len(jobs), 3)
        self.assertTrue(all(job.params == {} for job in jobs))
        jobs[0].params = {"channels": ["inapp"]}
        db.session.commit()
        seed_builtin_jobs_for_user(user)
        self.assertEqual(jobs[0].params, {"channels": ["inapp"]})

    def test_manual_without_request_uses_job_owner_and_restores_thread_scope(self):
        owner = self.make_user("owner")
        caller = self.make_user("caller")
        job = self._job(owner)
        set_setting("review_owner_setting", "owner value", user_id=owner.id)
        set_setting("review_owner_setting", "caller value", user_id=caller.id)
        scoping.set_current_user_id(caller.id)
        seen = []

        def action(user, params):
            seen.append((user.id, scoping.current_user_id(), get_setting("review_owner_setting")))

        with patch.dict(ACTIONS, {job.action: action}):
            self.assertEqual(SchedulerService(self.app).run_now(job.id), "成功（手动）")
        self.assertEqual(seen, [(owner.id, owner.id, "owner value")])
        self.assertEqual(scoping.current_user_id(), caller.id)

    def test_manual_request_does_not_leave_login_user_in_thread_scope(self):
        owner = self.make_user()
        job = self._job(owner)
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.app.scheduler = SchedulerService(self.app)
        seen = []
        with patch.dict(ACTIONS, {job.action: lambda user, params: seen.append(scoping.current_user_id())}):
            response = self.client_for(owner).post(f"/jobs/run/{job.id}")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(seen, [owner.id])
        self.assertIsNone(getattr(scoping._local, "user_id", None))

    def test_manual_long_error_is_persisted_without_masking_original_exception(self):
        owner = self.make_user("owner")
        caller = self.make_user("caller")
        job = self._job(owner)
        job_id = job.id
        scoping.set_current_user_id(caller.id)
        failure = ValueError("飞书推送失败：" + "用于验证数据库长度限制的详细错误" * 10)

        def fail(user, params):
            self.assertEqual(scoping.current_user_id(), owner.id)
            raise failure

        with patch.dict(ACTIONS, {job.action: fail}), patch("app.scheduler.logger.exception"):
            with self.assertRaises(ValueError) as caught:
                SchedulerService(self.app).run_now(job_id)
        self.assertIs(caught.exception, failure)
        self.assertEqual(scoping.current_user_id(), caller.id)
        db.session.expire_all()
        saved = db.session.get(ScheduledJob, job_id)
        self.assertTrue(saved.last_status.startswith("失败:"))
        self.assertIn("详见通知中心", saved.last_status)
        self.assertLessEqual(len(saved.last_status), 32)
        self.assertIsNotNone(saved.last_run_utc)

    def test_scheduled_long_error_is_persisted_and_scope_restored(self):
        owner = self.make_user("owner")
        caller = self.make_user("caller")
        job = self._job(owner)
        job_id = job.id
        scoping.set_current_user_id(caller.id)

        def fail(user, params):
            self.assertEqual(scoping.current_user_id(), owner.id)
            raise RuntimeError("通知发送失败 " * 40)

        with patch.dict(ACTIONS, {job.action: fail}), patch("app.scheduler.logger.exception"):
            _job_func(job_id, self.app)
        self.assertEqual(scoping.current_user_id(), caller.id)
        db.session.expire_all()
        saved = db.session.get(ScheduledJob, job_id)
        self.assertTrue(saved.last_status.startswith("失败:"))
        self.assertIn("详见通知中心", saved.last_status)
        self.assertLessEqual(len(saved.last_status), 32)

    def test_unregistered_long_action_cannot_overflow_status_column(self):
        owner = self.make_user()
        job = self._job(owner, action="missing_action_" * 4)
        job_id = job.id
        _job_func(job_id, self.app)
        db.session.expire_all()
        status = db.session.get(ScheduledJob, job_id).last_status
        self.assertTrue(status.startswith("失败:"))
        self.assertLessEqual(len(status), 32)

    def _update_channels(self, user, job_id, channels):
        self.app.config["WTF_CSRF_ENABLED"] = False
        form = MultiDict([
            ("cron_type", "cron5"), ("cron_min", "15"), ("cron_hour", "13"),
            ("enabled", "1"), *[("channels", channel) for channel in channels],
        ])
        response = self.client_for(user).post(f"/jobs/update/{job_id}", data=form)
        self.assertEqual(response.status_code, 302)
        db.session.expire_all()
        return db.session.get(ScheduledJob, job_id)

    def test_default_selection_removes_legacy_channel_override(self):
        owner = self.make_user()
        job = self._job(owner, params={"channel": "inapp", "keep": "value"})
        saved = self._update_channels(owner, job.id, [""])
        self.assertEqual(saved.params, {"keep": "value"})

    def test_explicit_inapp_selection_is_retained(self):
        owner = self.make_user()
        job = self._job(owner, params={"channel": "feishu"})
        saved = self._update_channels(owner, job.id, ["inapp"])
        self.assertEqual(saved.params, {"channels": ["inapp"]})

    def test_jobs_page_preserves_all_briefing_channels_when_editing_schedule(self):
        owner = self.make_user()
        channels = ["feishu_app", "serverchan", "inapp"]
        job = self._job(owner, params={"channels": channels})
        job_id = job.id
        html = self.client_for(owner).get("/jobs/").get_data(as_text=True)
        for channel in channels:
            self.assertIn(
                f'name="channels" value="{channel}" form="job-update-{job_id}"', html,
            )
        self.assertIn("/settings/?tab=briefing", html)
        saved = self._update_channels(owner, job_id, channels)
        self.assertEqual(saved.params["channels"], channels)
        self.assertEqual(saved.cron, "15 13 * * *")

    def test_deleting_notify_group_does_not_clear_another_users_job(self):
        from app.services.notify_service import delete_notify_group

        owner = self.make_user("owner")
        other = self.make_user("other")
        reference = "group:日常推送"
        owner_job = self._job(owner, params={"channels": [reference, "inapp"]})
        other_job = self._job(other, params={"channels": [reference]})
        owner_job_id, other_job_id = owner_job.id, other_job.id
        for user in (owner, other):
            set_setting("notify_groups", {"日常推送": ["feishu_app"]}, user_id=user.id)
            set_setting("default_channels", [reference], user_id=user.id)
            set_setting("notify_briefing_channels", [reference], user_id=user.id)
        with scoping.user_scope(owner.id):
            delete_notify_group("日常推送")
        db.session.expire_all()
        self.assertEqual(db.session.get(ScheduledJob, owner_job_id).params, {"channels": ["inapp"]})
        self.assertEqual(db.session.get(ScheduledJob, other_job_id).params, {"channels": [reference]})
        self.assertEqual(get_setting("notify_groups", user_id=other.id), {"日常推送": ["feishu_app"]})
        self.assertEqual(get_setting("default_channels", user_id=other.id), [reference])
        self.assertEqual(get_setting("notify_briefing_channels", user_id=other.id), [reference])
