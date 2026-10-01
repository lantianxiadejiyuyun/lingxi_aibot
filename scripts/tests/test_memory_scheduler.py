"""离线回归：增量摘要、稳定的调度计划与技能子进程任务同步。"""
from datetime import datetime, timedelta
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.tests.support import IsolatedAppTestCase

from app.extensions import db
from app.models.conversation import Conversation, Message
from app.models.scheduled_job import ScheduledJob
from app.scheduler import SchedulerService
from app.services import context_service, job_service, memory_service, skill_service
from app.utils.scoping import clear_current_user_id, set_current_user_id


class MemorySchedulerTests(IsolatedAppTestCase):
    def _conversation(self, summary=None):
        user = self.make_user()
        conv = Conversation(user_id=user.id, title="摘要回归", summary=summary)
        db.session.add(conv)
        db.session.flush()
        for i in range(14):
            db.session.add(Message(
                conversation_id=conv.id, role="user",
                content="OLD_UNIQUE_FACT" if i == 0 else f"message-{i}",
            ))
        db.session.commit()
        return conv, user

    def test_second_summary_keeps_previous_history(self):
        conv, user = self._conversation()
        llm = Mock(is_configured=True)
        llm.chat.side_effect = lambda messages: (
            "OLD_UNIQUE_FACT" if "OLD_UNIQUE_FACT" in messages[-1]["content"] else "lost", []
        )
        with patch("app.ai.llm.LLMClient", return_value=llm), \
                patch("app.ai.prompts.build_system_prompt", return_value="system"):
            self.assertTrue(memory_service.consolidate_conversation(conv, user))
            self.assertEqual(Message.query.filter_by(conversation_id=conv.id).count(), 14)
            self.assertEqual(len(context_service.active_messages(conv, user)), 8)
            for i in range(6):
                db.session.add(Message(conversation_id=conv.id, role="user", content=f"new-{i}"))
            db.session.commit()
            self.assertTrue(memory_service.consolidate_conversation(conv, user))
        self.assertIn("OLD_UNIQUE_FACT", llm.chat.call_args.args[0][-1]["content"])
        self.assertIn("OLD_UNIQUE_FACT", conv.summary)
        self.assertEqual(Message.query.filter_by(conversation_id=conv.id).count(), 20)
        self.assertEqual(len(context_service.active_messages(conv, user)), 8)

    def test_failed_summary_preserves_history_and_messages(self):
        from app.ai.llm import LLMError

        conv, user = self._conversation(summary="existing history")
        llm = Mock(is_configured=True)
        llm.chat.side_effect = LLMError("offline failure")
        with patch("app.ai.llm.LLMClient", return_value=llm), \
                patch("app.ai.prompts.build_system_prompt", return_value="system"):
            self.assertFalse(memory_service.consolidate_conversation(conv, user))
        self.assertEqual(conv.summary, "existing history")
        self.assertEqual(Message.query.filter_by(conversation_id=conv.id).count(), 14)

    def _start_scheduler(self):
        service = SchedulerService(self.app)
        service.sync()
        service.scheduler.start(paused=True)
        self.app.scheduler = service
        self.addCleanup(service.shutdown)
        return service

    def test_unmodified_intervals_keep_next_run_after_other_edits(self):
        user = self.make_user()
        job = job_service.create_reminder_job(user.id, "interval", "interval:10", "title", "body")
        job_id = job.id
        service = self._start_scheduler()
        before = service.next_run_time(job_id)

        class LaterDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(minutes=9)

        with patch("apscheduler.triggers.interval.datetime", LaterDatetime):
            job_service.create_reminder_job(user.id, "other", "0 9 * * *", "title", "body")
            service.sync()
        self.assertEqual(service.next_run_time(job_id), before)
        self.assertEqual(len(service.scheduler.get_jobs()), 2)

    def test_changed_plan_disable_and_reenable_are_synced(self):
        user = self.make_user()
        job = job_service.create_reminder_job(user.id, "interval", "interval:10", "title", "body")
        job_id = job.id
        service = self._start_scheduler()
        before = service.next_run_time(job_id)
        job_service.update_job(job, cron="interval:20")
        self.assertGreater(service.next_run_time(job_id), before + timedelta(minutes=9))
        job_service.update_job(job, enabled=False)
        self.assertIsNone(service.next_run_time(job_id))
        job_service.update_job(job, enabled=True)
        self.assertIsNotNone(service.next_run_time(job_id))
        job_service.delete_job(job)
        self.assertIsNone(service.next_run_time(job_id))

    def test_worker_like_skill_can_create_update_and_delete_jobs(self):
        user = self.make_user()
        # worker 构造的是最小 Flask app，没有活跃调度器。
        del self.app.scheduler
        set_current_user_id(user.id)
        self.addCleanup(clear_current_user_id)
        schema = {"type": "object", "properties": {}, "required": []}
        fn = skill_service.compile_skill_fn("review_jobs", schema, (
            'job = job_service.create_reminder_job(0, "created", "interval:10", "title", "body")\n'
            'job_service.update_job(job, cron="interval:20")\n'
            'job_id = job.id\n'
            'job_service.delete_job(job)\n'
            'return job_id'
        ))
        self.assertIsInstance(fn(), int)
        self.assertEqual(ScheduledJob.query.count(), 0)

    def test_skill_parent_syncs_committed_jobs_on_success_failure_and_timeout(self):
        user = self.make_user()
        service = self._start_scheduler()
        skill = SimpleNamespace(name="review_jobs", parameters={}, code="return 1")
        outcomes = (
            (subprocess.CompletedProcess([], 0, json.dumps({"result": "ok"}).encode(), b""), None),
            (subprocess.CompletedProcess([], 0, json.dumps({"error": "ValueError: failed"}).encode(), b""), ValueError),
            (subprocess.TimeoutExpired("worker", 10), skill_service.SkillError),
        )
        for index, (outcome, error_type) in enumerate(outcomes):
            with self.subTest(outcome=index):
                def completed_worker(*args, **kwargs):
                    # 模拟 worker 已提交，刻意不调用 job_service 的主进程同步。
                    db.session.add(ScheduledJob(
                        user_id=user.id, job_key=f"worker-{index}", name="worker",
                        action="custom_reminder", cron="0 9 * * *", enabled=True,
                        is_builtin=False, params={},
                    ))
                    db.session.commit()
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome

                with patch("subprocess.run", side_effect=completed_worker):
                    if error_type is None:
                        self.assertEqual(skill_service._run_in_subprocess(skill, {}), "ok")
                    else:
                        with self.assertRaises(error_type):
                            skill_service._run_in_subprocess(skill, {})
                self.assertEqual(len(service.scheduler.get_jobs()), index + 1)

    def test_valid_skill_schema_optional_before_required(self):
        schema = {
            "type": "object", "properties": {
                "optional": {"type": "string"}, "required": {"type": "string"},
            }, "required": ["required"],
        }
        fn = skill_service.compile_skill_fn("review_schema", schema, "return [required, optional]")
        self.assertEqual(fn(required="value"), ["value", None])
        self.assertEqual(fn(required="value", optional="extra"), ["value", "extra"])

    def test_forbidden_skill_patterns_keep_clear_error_messages(self):
        schema = {"type": "object", "properties": {}, "required": []}
        for code, feature in (("import os", "import"),
                              ("breakpoint()", r"breakpoint\s*\("),
                              ("return value.__class__", "__class__")):
            with self.subTest(code=code):
                with self.assertRaises(skill_service.SkillError) as caught:
                    skill_service.compile_skill_fn("review_forbidden", schema, code)
                self.assertEqual(str(caught.exception), f"技能代码包含被禁止的特征：{feature}")

    def test_folded_and_joined_strings_still_reject_forbidden_patterns(self):
        schema = {"type": "object", "properties": {}, "required": []}
        cases = (
            ('return "__glo" + "bals__"', "字符串常量", "__globals__"),
            (r'return "\x69mport"', "字符串常量", "import"),
            ('left = "__glo"\nright = "bals__"\nreturn left + right', "字符串常量拼接后", "__globals__"),
        )
        for code, stage, feature in cases:
            with self.subTest(stage=stage, code=code):
                with self.assertRaises(skill_service.SkillError) as caught:
                    skill_service.compile_skill_fn("review_forbidden", schema, code)
                self.assertEqual(str(caught.exception), f"技能代码{stage}包含被禁止的特征：{feature}")
