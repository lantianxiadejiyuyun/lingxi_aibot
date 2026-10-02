"""Declared model windows: large-history retention and complete-request limits."""
import json
from unittest.mock import Mock, patch

from scripts.tests.support import IsolatedAppTestCase
from app.extensions import db
from app.models.conversation import Conversation, Message
from app.services import context_service as context
from app.services.model_capabilities import (
    CONTEXT_WINDOW_OPTIONS, ContextWindowError, assert_context_fits,
    context_budget, estimate_tokens,
)
from app.utils import scoping


class ContextWindowTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        scoping.set_current_user_id(self.user.id)
        self.conv = Conversation(user_id=self.user.id, title="上下文容量")
        db.session.add(self.conv)
        db.session.commit()
        self.cfg = {
            "protocol": "openai", "base_url": "https://model.example/v1",
            "model": "large-model", "context_window_tokens": 1000000,
        }
        self.llm = Mock(is_configured=True)
        self.llm._read_config.side_effect = lambda: dict(self.cfg)
        self.llm.chat.return_value = ("保留重要事实", [])
        client_patch = patch("app.ai.llm.LLMClient", return_value=self.llm)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        prompt_patch = patch("app.ai.prompts.build_system_prompt", return_value="system")
        prompt_patch.start()
        self.addCleanup(prompt_patch.stop)

    def add_messages(self, count, text="历史内容", first=None):
        rows = []
        for i in range(count):
            row = Message(conversation_id=self.conv.id,
                          role="user" if i % 2 == 0 else "assistant",
                          content=first if i == 0 and first is not None else f"{i}: {text}")
            db.session.add(row)
            rows.append(row)
        db.session.commit()
        return rows

    def test_all_declared_windows_reserve_generation_and_system_tools(self):
        self.assertEqual([value for value, _ in CONTEXT_WINDOW_OPTIONS],
                         [32000, 64000, 128000, 256000, 512000, 1000000, 2000000])
        for window, _ in CONTEXT_WINDOW_OPTIONS:
            budget = context_budget({**self.cfg, "context_window_tokens": window})
            self.assertEqual(budget["context_window_tokens"], window)
            self.assertFalse(budget["legacy_context_policy"])
            self.assertEqual(budget["input_budget_tokens"] + budget["output_reserve_tokens"]
                             + budget["system_tools_reserve_tokens"], window)
            self.assertTrue(budget["token_estimate_is_approximate"])
        self.assertEqual(context_budget({"context_window_tokens": 345678})["context_window_tokens"], 345678)
        for undeclared in (None, 0, "0", ""):
            self.assertTrue(context_budget({"context_window_tokens": undeclared})["legacy_context_policy"])
        for invalid in (True, False, -1, "wrong", "1.5"):
            with self.assertRaises(ValueError):
                context_budget({"context_window_tokens": invalid})

    def test_million_window_keeps_history_beyond_old_both_thresholds(self):
        rows = self.add_messages(40, text="长历史事实" * 120)
        report = context.maybe_compact_conversation(self.conv, self.user)
        self.assertFalse(report["changed"])
        self.llm.chat.assert_not_called()
        self.assertEqual([row.id for row in context.active_messages(self.conv, self.user)],
                         [row.id for row in rows])
        status = context.context_status(self.conv, self.user)
        self.assertEqual(status["context_window_tokens"], 1000000)
        self.assertIsNone(status["auto_threshold_messages"])
        self.assertIsNone(status["auto_threshold_characters"])
        self.assertGreater(status["estimated_tokens"], 16000)
        self.assertLess(status["estimated_tokens"], status["auto_threshold_tokens"])

    def test_switch_to_smaller_model_triggers_compaction_by_token_budget(self):
        rows = self.add_messages(24, text="保留历史事实" * 200)
        self.assertFalse(context.maybe_compact_conversation(self.conv, self.user)["changed"])
        self.cfg["context_window_tokens"] = 32000
        report = context.maybe_compact_conversation(self.conv, self.user)
        self.assertTrue(report["changed"], report)
        self.assertEqual(Message.query.count(), len(rows))
        active = context.active_messages(self.conv, self.user)
        self.assertEqual(active[0].role, "user")
        self.assertEqual(active[-1].id, rows[-1].id)
        self.assertLess(len(active), len(rows))
        for call in self.llm.chat.call_args_list:
            assert_context_fits(call.args[0], cfg=self.cfg)

    def test_scheduled_consolidation_keeps_million_window_history_below_token_threshold(self):
        from app.services import memory_service

        rows = self.add_messages(40, text="长历史事实" * 120)
        with patch.object(memory_service, "extract_memories", return_value=0), \
                patch("app.services.rag_service.index_text") as index:
            report = memory_service.consolidate_all(self.user)
        self.assertEqual(report["summarized"], 0)
        self.llm.chat.assert_not_called()
        index.assert_not_called()
        self.assertIsNone(self.conv.summary)
        self.assertEqual([row.id for row in context.active_messages(self.conv, self.user)],
                         [row.id for row in rows])

    def test_scheduled_small_window_checks_few_long_messages_and_only_current_owner(self):
        from app.services import memory_service

        self.cfg["context_window_tokens"] = 32000
        rows = self.add_messages(4, first="OLDER_LONG_FACT " + "龘" * 15000)
        other = self.make_user("other-consolidation-owner")
        foreign = Conversation(user_id=other.id, title="foreign long conversation")
        db.session.add(foreign)
        db.session.flush()
        for i in range(4):
            db.session.add(Message(conversation_id=foreign.id,
                                   role="user" if i % 2 == 0 else "assistant",
                                   content="FOREIGN_PRIVATE_HISTORY" * 1000))
        db.session.commit()
        with patch.object(memory_service, "extract_memories", return_value=0), \
                patch("app.services.rag_service.index_text") as index:
            report = memory_service.consolidate_all(self.user)
        self.assertEqual(report["summarized"], 1)
        self.assertTrue(self.conv.summary)
        self.assertIsNone(foreign.summary)
        index.assert_called_once_with("conversation", self.conv.id, self.conv.summary)
        self.assertEqual(Message.query.filter_by(conversation_id=self.conv.id).count(), 4)
        active = context.active_messages(self.conv, self.user)
        self.assertEqual([row.id for row in active], [row.id for row in rows[-2:]])
        prompts = "\n".join(call.args[0][-1]["content"] for call in self.llm.chat.call_args_list)
        self.assertIn("OLDER_LONG_FACT", prompts)
        self.assertNotIn("FOREIGN_PRIVATE_HISTORY", prompts)

    def test_scheduled_undeclared_window_retains_legacy_twelve_message_cutoff(self):
        from app.services import memory_service

        self.cfg["context_window_tokens"] = 0
        self.add_messages(12)
        self.assertFalse(memory_service.consolidate_conversation(self.conv, self.user))
        self.llm.chat.assert_not_called()
        self.add_messages(2)
        self.assertTrue(memory_service.consolidate_conversation(self.conv, self.user))
        self.assertEqual(Message.query.filter_by(conversation_id=self.conv.id).count(), 14)
        self.assertEqual(len(context.active_messages(self.conv, self.user)), 8)

    def test_budget_split_stops_at_latest_user_even_when_that_turn_cannot_fit(self):
        self.cfg["context_window_tokens"] = 32000
        rows = self.add_messages(5, first="OLDER_FACT " + "龘" * 15000)
        rows[-1].content = "LATEST_INPUT_MUST_REMAIN " + "大" * 25000
        db.session.commit()
        report = context.maybe_compact_conversation(self.conv, self.user)
        self.assertTrue(report["changed"], report)
        active = context.active_messages(self.conv, self.user)
        self.assertEqual([row.id for row in active], [rows[-1].id])
        self.assertEqual(active[0].role, "user")
        self.assertEqual(Message.query.filter_by(conversation_id=self.conv.id).count(), 5)
        prompts = "\n".join(call.args[0][-1]["content"] for call in self.llm.chat.call_args_list)
        self.assertNotIn("LATEST_INPUT_MUST_REMAIN", prompts)
        with self.assertRaises(ContextWindowError):
            assert_context_fits([{"role": active[0].role, "content": active[0].content}], cfg=self.cfg)

    def test_small_window_chunks_large_original_without_losing_tail(self):
        self.cfg["context_window_tokens"] = 32000
        original = "START " + "龘" * 30000 + " END_FACT"
        rows = self.add_messages(14, first=original)
        report = context.maybe_compact_conversation(self.conv, self.user)
        self.assertTrue(report["changed"], report)
        prompts = [call.args[0][-1]["content"] for call in self.llm.chat.call_args_list]
        self.assertGreater(len(prompts), 3)
        self.assertIn("END_FACT", "".join(prompts))
        self.assertEqual("".join(prompts).count("龘"), 30000)
        self.assertEqual(db.session.get(Message, rows[0].id).content, original)
        for call in self.llm.chat.call_args_list:
            assert_context_fits(call.args[0], cfg=self.cfg)

    def test_oversize_latest_turn_reports_limit_without_hiding_history(self):
        self.cfg["context_window_tokens"] = 32000
        rows = self.add_messages(1, text="大" * 25000)
        report = context.maybe_compact_conversation(self.conv, self.user)
        self.assertFalse(report["ok"])
        self.assertTrue(report["context_exceeded"])
        self.assertIn("最新完整对话", report["message"])
        self.llm.chat.assert_not_called()
        self.assertEqual(context.active_messages(self.conv, self.user)[0].id, rows[0].id)
        with self.assertRaisesRegex(ContextWindowError, "不会扩展模型本身"):
            assert_context_fits([{"role": "user", "content": rows[0].content}], cfg=self.cfg)

    def test_actual_system_tools_and_tool_results_count_toward_request(self):
        self.cfg["context_window_tokens"] = 32000
        messages = [{"role": "system", "content": "s" * 20000},
                    {"role": "user", "content": "hello"}]
        tools = [{"type": "function", "function": {"description": "d" * 30000}}]
        with self.assertRaises(ContextWindowError):
            assert_context_fits(messages, tools, self.cfg)
        messages.append({"role": "tool", "content": "result" * 7000})
        with self.assertRaises(ContextWindowError):
            assert_context_fits(messages, cfg=self.cfg)
        self.cfg["context_window_tokens"] = 2000000
        self.assertLess(assert_context_fits(messages, tools, self.cfg)["request_estimated_tokens"],
                        context_budget(self.cfg)["request_input_budget_tokens"])

    def test_token_estimates_handle_unicode_images_and_thinking_reserve(self):
        self.assertGreater(estimate_tokens("中文🙂"), estimate_tokens("abc"))
        short_image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,a"}}
        long_image = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "a" * 500000}}
        self.assertEqual(estimate_tokens(short_image), estimate_tokens(long_image))
        budget = context_budget({"protocol": "anthropic", "base_url": "https://api.anthropic.com",
                                 "model": "claude-sonnet-4-5", "reasoning_effort": "high",
                                 "context_window_tokens": 128000})
        self.assertEqual(budget["output_reserve_tokens"], 16384)

    def test_status_reads_owner_config_and_restores_existing_user_scope(self):
        other = self.make_user("scope-other")
        scoping.set_current_user_id(other.id)
        seen = []
        self.llm._read_config.side_effect = lambda: seen.append(scoping.current_user_id()) or dict(self.cfg)
        status = context.context_status(self.conv, self.user)
        self.assertEqual(status["context_window_tokens"], 1000000)
        self.assertEqual(seen, [self.user.id])
        self.assertEqual(scoping.current_user_id(), other.id)

    def test_executor_rechecks_current_window_after_model_switch_tool(self):
        from app.ai.executor import run_chat

        self.cfg["context_window_tokens"] = 2000000
        self.llm.chat_stream.return_value = iter([{
            "type": "tool_calls", "calls": [{"id": "switch", "name": "switch_chat_model", "arguments": "{}"}],
        }])

        def switch(*args):
            self.cfg["context_window_tokens"] = 32000
            return json.dumps({"changed": True, "message": "selected smaller model", "result": "x" * 60000})

        with patch("app.ai.executor.LLMClient", return_value=self.llm), \
                patch("app.ai.executor.ack_received", return_value=""), \
                patch("app.services.model_control_service.runtime_prompt", return_value="configuration"), \
                patch("app.ai.executor.registry.openai_tools", return_value=[]), \
                patch("app.ai.executor.registry.execute_tool", side_effect=switch):
            events = list(run_chat(self.conv, "hello", self.user))
        errors = [text for kind, text in events if kind == "error"]
        self.assertTrue(errors, events)
        self.assertIn("32,000", errors[0])
        self.assertEqual(self.llm.chat_stream.call_count, 1)
        self.assertEqual(Message.query.filter_by(conversation_id=self.conv.id, role="user").count(), 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
