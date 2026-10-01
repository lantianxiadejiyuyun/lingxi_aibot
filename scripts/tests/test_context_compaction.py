"""上下文压缩回归：历史保留、增量边界、失败原子性、完整轮次与大消息。"""
from unittest.mock import Mock, patch

from scripts.tests.support import IsolatedAppTestCase
from app.ai.llm import LLMError
from app.ai.memory import build_messages
from app.extensions import db
from app.models.conversation import Conversation, Message
from app.models.setting import Setting
from app.services import context_service as context
from app.utils import scoping


class ContextCompactionTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.conv = Conversation(user_id=self.user.id, title="上下文回归")
        db.session.add(self.conv)
        db.session.commit()
        self.llm = Mock(is_configured=True)
        self.llm.chat.return_value = ("保留已有重要事实", [])
        self.client_patch = patch("app.ai.llm.LLMClient", return_value=self.llm)
        self.client_mock = self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.prompt_patch = patch("app.ai.prompts.build_system_prompt", return_value="system")
        self.prompt_patch.start()
        self.addCleanup(self.prompt_patch.stop)
        scoping.clear_current_user_id()
        self.addCleanup(scoping.clear_current_user_id)

    def add_messages(self, count, first=None):
        start = Message.query.filter_by(conversation_id=self.conv.id).count()
        rows = []
        for i in range(count):
            row = Message(conversation_id=self.conv.id,
                          role="user" if (start + i) % 2 == 0 else "assistant",
                          content=first if first is not None and i == 0 else f"message-{start+i}")
            rows.append(row)
            db.session.add(row)
        db.session.commit()
        return rows

    def state(self):
        return Setting.query.filter_by(key=f"chat_context:{self.conv.id}",
                                       user_id=self.user.id).first()

    def test_two_compactions_keep_all_history_and_summarize_only_new_prefix(self):
        rows = self.add_messages(24, first="INITIAL_FACT")
        self.llm.chat.return_value = ("INITIAL_FACT", [])
        first = context.compact_conversation(self.conv, self.user)
        self.assertTrue(first["changed"])
        self.assertEqual(first["after_messages"], 8)
        self.assertEqual(self.state().value["through_id"], rows[15].id)
        self.assertEqual(Message.query.count(), 24)

        self.add_messages(10)
        self.llm.chat.reset_mock()
        with context.conversation_lock(self.conv.id):
            second = context.compact_conversation(self.conv, self.user, force=True)
        self.assertTrue(second["changed"])
        self.assertEqual(second["before_messages"], 18)
        self.assertEqual(Message.query.count(), 34)
        prompt = self.llm.chat.call_args.args[0][-1]["content"]
        self.assertIn("INITIAL_FACT", prompt)
        self.assertIn("message-16", prompt)
        self.assertNotIn("message-2\n", prompt)
        self.assertNotIn("message-33", prompt)
        active = context.active_messages(self.conv, self.user)
        self.assertEqual([m.content for m in active], [f"message-{i}" for i in range(26, 34)])

    def test_compaction_failure_keeps_previous_summary_boundary_and_history(self):
        self.add_messages(24)
        context.compact_conversation(self.conv, self.user)
        old_summary = self.conv.summary
        old_boundary = dict(self.state().value)
        self.add_messages(24)
        self.llm.chat.side_effect = LLMError("test failure")
        report = context.compact_conversation(self.conv, self.user)
        self.assertFalse(report["ok"])
        self.assertEqual(self.conv.summary, old_summary)
        self.assertEqual(self.state().value, old_boundary)
        self.assertEqual(Message.query.count(), 48)
        self.assertEqual(len(context.active_messages(self.conv, self.user)), 32)

    def test_large_single_message_is_processed_fully_in_bounded_chunks(self):
        text = "START_FACT" + "甲" * 14000 + "MIDDLE_FACT" + "乙" * 14000 + "TAIL_FACT"
        rows = self.add_messages(14, first=text)
        report = context.compact_conversation(self.conv, self.user)
        self.assertTrue(report["changed"])
        prompts = [call.args[0][-1]["content"] for call in self.llm.chat.call_args_list]
        self.assertGreaterEqual(len(prompts), 3)
        self.assertLess(max(map(len, prompts)), 20000)
        joined = "\n".join(prompts)
        for fact in ("START_FACT", "MIDDLE_FACT", "TAIL_FACT"):
            self.assertIn(fact, joined)
        self.assertEqual(joined.count("甲"), 14000)
        self.assertEqual(joined.count("乙"), 14000)
        self.assertEqual(db.session.get(Message, rows[0].id).content, text)

    def test_failure_on_later_chunk_does_not_save_partial_summary(self):
        self.add_messages(14, first="a" * 30000)
        self.conv.summary = "previous summary"
        db.session.commit()
        self.llm.chat.side_effect = [("partial summary", []), LLMError("chunk two failed")]
        report = context.compact_conversation(self.conv, self.user)
        self.assertFalse(report["ok"])
        self.assertEqual(self.conv.summary, "previous summary")
        self.assertIsNone(self.state())
        self.assertEqual(len(context.active_messages(self.conv, self.user)), 14)

    def test_empty_and_oversized_summaries_do_not_hide_messages(self):
        self.add_messages(24)
        for summary in ("", "x" * (context.MAX_SUMMARY_CHARACTERS + 1)):
            with self.subTest(size=len(summary)):
                self.llm.chat.return_value = (summary, [])
                report = context.compact_conversation(self.conv, self.user)
                self.assertFalse(report["ok"])
                self.assertIsNone(self.state())
                self.assertEqual(len(context.active_messages(self.conv, self.user)), 24)

    def test_expanding_short_summary_is_retried_once_then_rejected(self):
        rows = self.add_messages(18)
        self.llm.chat.return_value = ("额外扩写的背景解释。" * 90, [])
        report = context.compact_conversation(self.conv, self.user, force=True)
        self.assertFalse(report["ok"])
        self.assertFalse(report["changed"])
        self.assertIn("未缩短", report["message"])
        self.assertEqual(self.llm.chat.call_count, 2)
        self.assertIsNone(self.conv.summary)
        self.assertIsNone(self.state())
        self.assertEqual(len(context.active_messages(self.conv, self.user)), 18)
        self.assertEqual(Message.query.count(), len(rows))
        budget = int(sum(len(m.content) for m in rows[:-8]) * 0.6)
        first_prompt = self.llm.chat.call_args_list[0].args[0][-1]["content"]
        self.assertIn(f"不超过 {budget} 个字符", first_prompt)

    def test_one_tightening_retry_can_produce_a_genuinely_smaller_context(self):
        rows = self.add_messages(18)
        before_characters = sum(len(m.content) for m in rows)
        expanded = "重复内容" * 80 + "TAIL_FACT"
        self.llm.chat.side_effect = [(expanded, []), ("TAIL_FACT", [])]
        report = context.compact_conversation(self.conv, self.user, force=True)
        self.assertTrue(report["ok"])
        self.assertTrue(report["changed"])
        self.assertEqual(self.llm.chat.call_count, 2)
        self.assertIn(expanded, self.llm.chat.call_args.args[0][-1]["content"])
        active = context.active_messages(self.conv, self.user)
        self.assertLess(len(self.conv.summary) + sum(len(m.content) for m in active), before_characters)
        self.assertEqual(self.conv.summary, "TAIL_FACT")
        self.assertEqual(self.state().value["through_id"], rows[-9].id)
        self.assertEqual(Message.query.count(), 18)

    def test_expansion_after_previous_compaction_preserves_summary_and_boundary(self):
        self.add_messages(24)
        self.assertTrue(context.compact_conversation(self.conv, self.user)["changed"])
        old_summary = self.conv.summary
        old_boundary = dict(self.state().value)
        self.add_messages(10)
        before_ids = [m.id for m in context.active_messages(self.conv, self.user)]
        replaced = len(old_summary) + sum(len(m.content) for m in context.active_messages(self.conv, self.user)[:-8])
        self.llm.chat.reset_mock()
        # 结果虽略短于原文，却没有达到 60% 的有效压缩预算，仍不能推进边界。
        self.llm.chat.return_value = ("长" * (replaced - 1), [])
        report = context.compact_conversation(self.conv, self.user, force=True)
        self.assertFalse(report["ok"])
        self.assertIn("未缩短", report["message"])
        self.assertEqual(self.llm.chat.call_count, 2)
        self.assertEqual(self.conv.summary, old_summary)
        self.assertEqual(self.state().value, old_boundary)
        self.assertEqual([m.id for m in context.active_messages(self.conv, self.user)], before_ids)
        self.assertEqual(Message.query.count(), 34)

    def test_only_final_chunk_uses_proportional_budget(self):
        self.add_messages(24)
        intermediate = "仍需与后续片段合并的重要事实；" * 20
        def summarize(messages):
            prompt = messages[-1]["content"]
            return ("最终关键事实" if "这是最终摘要。" in prompt else intermediate, [])
        self.llm.chat.side_effect = summarize
        with patch.object(context, "CHUNK_CHARACTERS", 80):
            report = context.compact_conversation(self.conv, self.user)
        self.assertTrue(report["changed"])
        self.assertGreater(self.llm.chat.call_count, 1)
        prompts = [call.args[0][-1]["content"] for call in self.llm.chat.call_args_list]
        self.assertTrue(all("不超过 6000 个字符" in prompt for prompt in prompts[:-1]))
        self.assertIn(intermediate, prompts[-1])
        self.assertEqual(self.conv.summary, "最终关键事实")

    def test_owner_is_required_even_when_global_or_foreign_state_exists(self):
        self.add_messages(24)
        other = self.make_user("context-other", False)
        db.session.add(Setting(key=f"chat_context:{self.conv.id}", user_id=0,
                               value={"through_id": 999999}))
        db.session.add(Setting(key=f"chat_context:{self.conv.id}", user_id=other.id,
                               value={"through_id": 999999}))
        self.conv.summary = "existing summary"
        db.session.commit()
        self.assertEqual(len(context.active_messages(self.conv, self.user)), 24)
        for function in (context.compact_conversation, context.active_messages, context.context_status):
            with self.assertRaises(ValueError):
                function(self.conv, other)
        self.llm.chat.assert_not_called()

    def test_message_and_character_thresholds_and_recent_turn_protection(self):
        self.add_messages(23)
        self.assertFalse(context.maybe_compact_conversation(self.conv, self.user)["changed"])
        self.llm.chat.assert_not_called()
        self.add_messages(1)
        self.assertTrue(context.maybe_compact_conversation(self.conv, self.user)["changed"])
        self.assertEqual(len(context.active_messages(self.conv, self.user)), 8)
        # 仅剩最近轮次，即使它很长也不丢弃当前用户问题。
        active = context.active_messages(self.conv, self.user)
        active[-2].content = "current user input" * 2000
        db.session.commit()
        self.assertFalse(context.maybe_compact_conversation(self.conv, self.user)["changed"])
        self.assertFalse(context.context_status(self.conv, self.user)["can_compact"])

    def test_keep_recent_window_starts_with_user_and_includes_current_turn(self):
        rows = self.add_messages(11)
        report = context.compact_conversation(self.conv, self.user, force=True)
        self.assertTrue(report["changed"])
        self.assertEqual(report["after_messages"], 9)
        active = context.active_messages(self.conv, self.user)
        self.assertEqual(active[0].id, rows[2].id)
        self.assertEqual(active[0].role, "user")
        self.assertEqual(active[-1].id, rows[-1].id)

    def test_messages_without_successful_summary_boundary_are_not_silently_truncated(self):
        self.add_messages(30, first="FIRST_UNSUMMARIZED_FACT")
        self.conv.summary = "legacy summary without boundary"
        db.session.commit()
        messages = build_messages(self.conv, self.user)
        self.assertEqual(len([m for m in messages if m["role"] in ("user", "assistant")]), 30)
        self.assertTrue(any(m["content"] == "FIRST_UNSUMMARIZED_FACT" for m in messages))

    def test_message_builder_refreshes_summary_together_with_its_boundary(self):
        from sqlalchemy.orm.attributes import set_committed_value

        self.add_messages(24)
        self.llm.chat.return_value = ("NEW_SUMMARY_WITH_PREFIX_FACTS", [])
        context.compact_conversation(self.conv, self.user)
        # 模拟会话在取得锁前已载入，而另一轮压缩已经推进了 DB 中的摘要与边界。
        set_committed_value(self.conv, "summary", "STALE_SUMMARY")
        messages = build_messages(self.conv, self.user)
        summary_messages = [m["content"] for m in messages if "本会话历史摘要" in m["content"]]
        self.assertEqual(summary_messages, ["本会话历史摘要：\nNEW_SUMMARY_WITH_PREFIX_FACTS"])
        self.assertEqual(len([m for m in messages if m["role"] in ("user", "assistant")]), 8)

    def test_missing_model_is_safe_and_temporary_user_context_is_restored(self):
        self.add_messages(24)
        other = self.make_user("scope-other", False)
        scoping.set_current_user_id(other.id)
        self.llm.is_configured = False
        report = context.compact_conversation(self.conv, self.user)
        self.assertFalse(report["ok"])
        self.assertEqual(scoping.current_user_id(), other.id)
        self.assertIsNone(self.state())
        self.llm.chat.assert_not_called()
        self.client_mock.assert_called_with(conversation=self.conv)

    def test_summary_and_boundary_are_atomic_on_commit_failure(self):
        self.add_messages(24)
        self.conv.summary = "old summary"
        db.session.commit()
        with patch.object(db.session, "commit", side_effect=RuntimeError("database unavailable")):
            result = context.compact_conversation(self.conv, self.user)
        self.assertFalse(result["ok"])
        self.assertEqual(self.conv.summary, "old summary")
        self.assertIsNone(self.state())
        self.assertEqual(len(context.active_messages(self.conv, self.user)), 24)
