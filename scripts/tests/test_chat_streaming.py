"""Offline regressions for text continuity, bounded tools and stream cleanup."""
import io
import json
import unittest
from unittest.mock import Mock, patch

from scripts.tests.support import IsolatedAppTestCase
from app.ai.executor import run_chat
from app.ai.llm import LLMClient, LLMError
from app.extensions import db
from app.models.conversation import Conversation, Message
from app.utils.scoping import set_current_user_id


CALL = {"id": "call-1", "name": "create_task", "arguments": '{"title":"test"}'}
CFG = {"protocol": "openai", "base_url": "https://model.invalid/v1",
       "api_key": "offline-key", "model": "test", "timeout": 30}


class ChatContinuityTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        set_current_user_id(self.user.id)
        self.conv = Conversation(user_id=self.user.id, title="stream test")
        db.session.add(self.conv)
        db.session.commit()
        self.llm = Mock(is_configured=True)
        self.llm._read_config.return_value = CFG
        replacements = {
            "app.ai.executor.LLMClient": {"return_value": self.llm},
            "app.ai.executor.ack_received": {"return_value": "收到"},
            "app.ai.executor._build_runtime_messages": {"return_value": [{"role": "user", "content": "test"}]},
            "app.ai.executor.registry.openai_tools": {"return_value": []},
            "app.ai.executor.registry.execute_tool": {"return_value": json.dumps({"id": 12})},
            "app.services.context_service.maybe_compact_conversation": {"return_value": {"changed": False}},
            "app.services.model_capabilities.assert_context_fits": {"return_value": None},
        }
        for target, options in replacements.items():
            stub = patch(target, **options)
            stub.start()
            self.addCleanup(stub.stop)

    def _history(self):
        return Message.query.filter_by(conversation_id=self.conv.id, role="assistant").one()

    def test_tool_round_text_matches_live_deltas_done_and_history(self):
        self.llm.chat_stream.side_effect = [
            iter([{"type": "delta", "text": "正在创建任务。"}, {"type": "tool_calls", "calls": [CALL]}]),
            iter([{"type": "delta", "text": "任务"}, {"type": "delta", "text": "已创建。"}]),
        ]
        events = list(run_chat(self.conv, "创建任务", self.user))
        displayed = "收到\n\n" + "".join(text for kind, text in events if kind == "delta")
        expected = "收到\n\n正在创建任务。\n\n任务已创建。"
        self.assertEqual(displayed, expected)
        self.assertIn(("done", expected), events)
        self.assertEqual(self._history().content, expected)
        self.assertEqual(self._history().tool_calls, [CALL])

    def test_tool_limit_is_explicit_even_when_last_round_has_a_preamble(self):
        self.app.config["LLM_MAX_TOOL_ROUNDS"] = 1
        self.llm.chat_stream.return_value = iter([
            {"type": "delta", "text": "正在处理。"}, {"type": "tool_calls", "calls": [CALL]},
        ])
        events = list(run_chat(self.conv, "创建任务", self.user))
        final = next(text for kind, text in events if kind == "done")
        self.assertIn("正在处理。\n\n", final)
        self.assertIn("达到上限", final)
        self.assertIn("请勿重复提交", final)
        self.assertEqual(self.llm.chat_stream.call_count, 1)
        self.assertEqual(final, "收到\n\n" + "".join(text for kind, text in events if kind == "delta"))

    def test_empty_response_reports_error_instead_of_successful_ack(self):
        self.llm.chat_stream.return_value = iter([{"type": "delta", "text": ""}])
        events = list(run_chat(self.conv, "你好", self.user))
        self.assertFalse(any(kind == "done" for kind, _ in events))
        self.assertTrue(any(kind == "error" and "未返回有效回复" in text for kind, text in events))
        self.assertIn("中断", self._history().content)
        self.assertNotIn("工具操作", self._history().content)

    def test_disconnected_generator_closes_upstream_and_retains_partial_text(self):
        closed = []

        def stream(*args, **kwargs):
            try:
                yield {"type": "delta", "text": "已经生成的内容"}
                yield {"type": "delta", "text": "尚未发送"}
            finally:
                closed.append(True)

        self.llm.chat_stream.side_effect = stream
        iterator = run_chat(self.conv, "你好", self.user)
        for kind, _ in iterator:
            if kind == "delta":
                break
        iterator.close()
        self.assertEqual(closed, [True])
        content = self._history().content
        self.assertIn("已经生成的内容", content)
        self.assertNotIn("尚未发送", content)
        self.assertIn("中断", content)

    def test_disconnect_after_tool_preserves_result_and_does_not_repeat_it(self):
        self.llm.chat_stream.return_value = iter([{"type": "tool_calls", "calls": [CALL]}])
        iterator = run_chat(self.conv, "创建任务", self.user)
        for kind, _ in iterator:
            if kind == "tool":
                break
        iterator.close()
        self.assertEqual(Message.query.filter_by(conversation_id=self.conv.id, role="tool").count(), 1)
        self.assertEqual(self._history().tool_calls, [CALL])
        self.assertIn("请勿重复提交", self._history().content)
        self.assertEqual(self.llm.chat_stream.call_count, 1)

    def test_provider_error_preserves_partial_text_and_completed_tool(self):
        def broken_stream():
            yield {"type": "delta", "text": "任务已经创建"}
            raise LLMError("upstream timeout")

        self.llm.chat_stream.side_effect = [
            iter([{"type": "tool_calls", "calls": [CALL]}]), broken_stream(),
        ]
        events = list(run_chat(self.conv, "创建任务", self.user))
        self.assertIn(("error", "upstream timeout"), events)
        self.assertIn("任务已经创建", self._history().content)
        self.assertIn("请勿重复提交", self._history().content)
        self.assertEqual(self._history().tool_calls, [CALL])
        self.assertEqual(Message.query.filter_by(conversation_id=self.conv.id, role="tool").count(), 1)

    def test_closing_after_done_does_not_add_an_interruption(self):
        self.llm.chat_stream.return_value = iter([{"type": "delta", "text": "完成"}])
        iterator = run_chat(self.conv, "你好", self.user)
        for kind, _ in iterator:
            if kind == "done":
                break
        iterator.close()
        self.assertEqual(self._history().content, "收到\n\n完成")

    def test_command_reply_is_saved_before_first_title_event(self):
        self.conv.title = "新对话"
        db.session.commit()
        iterator = run_chat(self.conv, "/help", self.user)
        self.assertEqual(next(iterator)[0], "title")
        iterator.close()
        db.session.expire_all()
        self.assertIn("/model", self._history().content)
        self.assertNotIn("中断", self._history().content)
        self.assertEqual(self.conv.title, "会话控制")


class ProviderStreamTests(unittest.TestCase):
    def _client(self, stream):
        sdk = Mock()
        sdk.chat.completions.create.return_value = stream
        client = LLMClient()
        for target, options in (
            ("_read_config", {"return_value": dict(CFG)}),
            ("_apply", {"return_value": None}),
            ("_client_for", {"return_value": sdk}),
        ):
            stub = patch.object(client, target, **options)
            stub.start()
            self.addCleanup(stub.stop)
        return client

    def test_openai_typed_text_is_rendered_and_response_is_closed(self):
        stream = Mock()
        stream.__iter__ = Mock(return_value=iter([{"choices": [{"delta": {"content": [
            {"type": "text", "text": "正文"}, {"type": "image_url", "image_url": "ignore"},
        ]}}]}]))
        result = list(self._client(stream).chat_stream([{"role": "user", "content": "test"}]))
        self.assertEqual(result, [{"type": "delta", "text": "正文"}])
        stream.close.assert_called_once()

    def test_openai_response_is_closed_when_consumer_stops_early(self):
        stream = Mock()
        stream.__iter__ = Mock(return_value=iter([
            {"choices": [{"delta": {"content": "first"}}]},
            {"choices": [{"delta": {"content": "second"}}]},
        ]))
        events = self._client(stream).chat_stream([{"role": "user", "content": "test"}])
        self.assertEqual(next(events)["text"], "first")
        events.close()
        stream.close.assert_called_once()

    def test_anthropic_bytes_are_decoded_without_buffering_small_deltas(self):
        response = Mock(status_code=200)
        response.iter_lines.return_value = [
            ('data: ' + json.dumps({"type": "content_block_delta", "index": 0,
                                  "delta": {"type": "text_delta", "text": "你好"}}, ensure_ascii=False)).encode("utf-8"),
        ]
        with patch("requests.post", return_value=response):
            result = list(LLMClient()._anthropic_stream(CFG, [{"role": "user", "content": "test"}], None))
        self.assertEqual(result[0], {"type": "delta", "text": "你好"})
        response.iter_lines.assert_called_once_with(chunk_size=1, decode_unicode=True)
        response.close.assert_called_once()

    def test_anthropic_http_error_closes_response(self):
        response = Mock(status_code=429)
        response.json.return_value = {"error": {"message": "rate limit"}}
        with patch("requests.post", return_value=response):
            with self.assertRaisesRegex(LLMError, "rate limit"):
                list(LLMClient()._anthropic_stream(CFG, [{"role": "user", "content": "test"}], None))
        response.close.assert_called_once()

    def test_anthropic_sse_without_charset_keeps_chinese_text(self):
        import requests

        response = requests.Response()
        response.status_code = 200
        response.encoding = "ISO-8859-1"  # requests' default for text/* without charset.
        response.raw = io.BytesIO(('data: ' + json.dumps({
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "中文回答"},
        }, ensure_ascii=False) + '\n\n').encode('utf-8'))
        with patch("requests.post", return_value=response):
            result = list(LLMClient()._anthropic_stream(CFG, [{"role": "user", "content": "test"}], None))
        self.assertEqual(result[0], {"type": "delta", "text": "中文回答"})


if __name__ == "__main__":
    unittest.main()
