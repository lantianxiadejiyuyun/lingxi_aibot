"""Provider payload and signed-tool-roundtrip regressions; no external requests."""
import json
import unittest
from unittest.mock import Mock, patch

from scripts.tests.support import IsolatedAppTestCase
from app.ai.llm import LLMClient, LLMError, PROVIDER_PRESETS, default_for_protocol, iter_anthropic_sse
from app.ai.reasoning import reasoning_levels, request_options, validate_reasoning


def config(model="gpt-5.1", level="high", protocol="openai", base="https://api.openai.com/v1"):
    return {"protocol": protocol, "base_url": base, "api_key": "sk-offline-test",
            "model": model, "timeout": 30, "reasoning_effort": level}


TOOL = {"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}


class ReasoningPayloadTests(unittest.TestCase):
    def test_deepseek_quick_fill_uses_verified_model_without_migrating_fallback(self):
        preset = next(p for p in PROVIDER_PRESETS if p["id"] == "deepseek")
        self.assertEqual(preset["model"], "deepseek-flash")
        self.assertEqual(default_for_protocol("openai")["model"], "deepseek-chat")
        self.assertEqual(default_for_protocol("anthropic")["model"], "claude-sonnet-4-5")

    def test_unknown_gateway_has_no_thinking_parameters(self):
        cfg = config(base="https://gateway.example/v1", level="default")
        self.assertEqual(reasoning_levels(cfg["protocol"], cfg["base_url"], cfg["model"]), ["default"])
        self.assertEqual(request_options(cfg), {"temperature": 0.7})
        with self.assertRaisesRegex(ValueError, "不支持"):
            validate_reasoning("high", cfg["protocol"], cfg["base_url"], cfg["model"])

    def test_openai_reasoning_removes_temperature_and_off_maps_to_none(self):
        self.assertEqual(request_options(config()), {"reasoning_effort": "high"})
        self.assertEqual(request_options(config(level="off")), {"reasoning_effort": "none"})
        self.assertEqual(request_options(config(model="o3", level="default")), {})
        with self.assertRaises(ValueError):
            request_options(config(model="gpt-5", level="off"))

    def test_openai_six_unsupported_tool_combination_is_explicit(self):
        with self.assertRaisesRegex(ValueError, "Responses API"):
            request_options(config(model="gpt-6-astra"), tools=True)
        self.assertEqual(request_options(config(model="gpt-6-sol", level="off"), tools=True),
                         {"reasoning_effort": "none"})

    def test_deepseek_only_exposes_distinct_efforts(self):
        cfg = config(model="deepseek-flash", base="https://api.deepseek.com/v1")
        self.assertEqual(reasoning_levels(cfg["protocol"], cfg["base_url"], cfg["model"]),
                         ["default", "off", "low", "high", "max"])
        self.assertEqual(request_options(cfg), {"reasoning_effort": "high", "extra_body": {"thinking": {"type": "enabled"}}})
        cfg["reasoning_effort"] = "off"
        self.assertEqual(request_options(cfg), {"extra_body": {"thinking": {"type": "disabled"}}})
        cfg["reasoning_effort"] = "medium"
        with self.assertRaises(ValueError):
            request_options(cfg)

    def test_qwen_hybrid_uses_budgets_and_thinking_only_refuses_off(self):
        cfg = config(model="qwen-plus", base="https://dashscope.aliyuncs.com/compatible-mode/v1", level="medium")
        self.assertEqual(request_options(cfg), {"extra_body": {"enable_thinking": True, "thinking_budget": 4096}})
        cfg.update(model="qwen3-235b-a22b-thinking-2507", reasoning_effort="off")
        with self.assertRaises(ValueError):
            request_options(cfg)
        cfg["reasoning_effort"] = "high"
        self.assertEqual(request_options(cfg), {"extra_body": {"thinking_budget": 8192}})

    def test_old_qwen_snapshot_is_not_assumed_thinking_capable(self):
        self.assertEqual(reasoning_levels("openai", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus-2025-01-25"), ["default"])

    def test_claude_manual_budget_leaves_answer_room(self):
        cfg = config(model="claude-sonnet-4-5", protocol="anthropic", base="https://api.anthropic.com")
        body = LLMClient()._anthropic_body(cfg, [{"role": "user", "content": "test"}], [TOOL], False)
        self.assertEqual(body["thinking"], {"type": "enabled", "budget_tokens": 8192})
        self.assertGreater(body["max_tokens"], body["thinking"]["budget_tokens"])
        self.assertNotIn("temperature", body)

    def test_claude_adaptive_and_always_on(self):
        cfg = config(model="claude-sonnet-4-6", protocol="anthropic", base="https://api.anthropic.com", level="low")
        self.assertEqual(request_options(cfg), {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}})
        cfg.update(model="claude-opus-5-5", reasoning_effort="off")
        with self.assertRaises(ValueError):
            request_options(cfg)

    def test_real_openai_sdk_serializes_effort_and_extra_body(self):
        import httpx
        from openai import OpenAI

        captured = []

        def handle(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "chatcmpl-test", "object": "chat.completion", "created": 1,
                "model": "test", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})

        cfg = config(model="deepseek-flash", base="https://api.deepseek.com/v1", level="low")
        client = LLMClient()
        transport_client = httpx.Client(transport=httpx.MockTransport(handle))
        sdk = OpenAI(api_key="sk-offline", base_url=cfg["base_url"], http_client=transport_client)
        self.addCleanup(sdk.close)
        history = [{"role": "user", "content": "test"}, {
            "role": "assistant", "content": "checking", "reasoning_content": "native-history",
            "_llm_signature": client._message_signature(cfg),
            "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
        }, {"role": "tool", "tool_call_id": "t1", "content": "sunny"}]
        with patch.object(client, "_read_config", return_value=cfg), \
                patch.object(client, "_build_openai", return_value=sdk), \
                patch("app.utils.urlsafety.check_provider_url", side_effect=lambda value: value):
            self.assertEqual(client.chat(history, [TOOL]), ("ok", []))
        self.assertEqual(captured[0]["reasoning_effort"], "low")
        self.assertEqual(captured[0]["thinking"], {"type": "enabled"})
        self.assertNotIn("extra_body", captured[0])
        self.assertNotIn("temperature", captured[0])
        self.assertEqual(captured[0]["messages"][1]["reasoning_content"], "native-history")
        self.assertNotIn("_llm_signature", captured[0]["messages"][1])

    def test_streamed_deepseek_reasoning_is_internal_and_round_trips(self):
        cfg = config(model="deepseek-flash", base="https://api.deepseek.com/v1")
        chunks = [
            {"choices": [{"delta": {"reasoning_content": "private "}}]},
            {"choices": [{"delta": {"reasoning_content": "work", "content": "answer"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "t1", "function": {"name": "get_weather", "arguments": "{}"}}]}}]},
        ]
        sdk = Mock()
        sdk.chat.completions.create.return_value = iter(chunks)
        client = LLMClient()
        with patch.object(client, "_read_config", return_value=cfg), \
                patch.object(client, "_build_openai", return_value=sdk), \
                patch("app.utils.urlsafety.check_provider_url", side_effect=lambda value: value):
            events = list(client.chat_stream([{"role": "user", "content": "test"}], [TOOL]))
        self.assertEqual([event["text"] for event in events if event["type"] == "delta"], ["answer"])
        metadata = next(event["data"] for event in events if event["type"] == "assistant_meta")
        msg = {"role": "assistant", "content": "answer", **metadata}
        self.assertEqual(client._prepare_messages(cfg, [msg])[0]["reasoning_content"], "private work")
        changed = {**cfg, "model": "deepseek-v4-pro"}
        self.assertNotIn("reasoning_content", client._prepare_messages(changed, [msg])[0])
        changed = {**cfg, "reasoning_effort": "low"}
        self.assertNotIn("reasoning_content", client._prepare_messages(changed, [msg])[0])
        self.assertNotIn("_llm_signature", client._prepare_messages(cfg, [msg])[0])

    def test_anthropic_signed_and_redacted_blocks_replay_in_original_order(self):
        events = [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "private"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-1"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "-2"}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "checking"}},
            {"type": "content_block_start", "index": 2, "content_block": {"type": "redacted_thinking", "data": "opaque"}},
            {"type": "content_block_start", "index": 3, "content_block": {"type": "tool_use", "id": "t1", "name": "get_weather", "input": {}}},
            {"type": "content_block_delta", "index": 3, "delta": {"type": "input_json_delta", "partial_json": '{"city":'}},
            {"type": "content_block_delta", "index": 3, "delta": {"type": "input_json_delta", "partial_json": '"Shanghai"}'}},
        ]
        cfg = config(model="claude-sonnet-4-5", protocol="anthropic", base="https://api.anthropic.com")
        client = LLMClient()
        response = Mock(status_code=200)
        response.iter_lines.return_value = ["data: " + json.dumps(event) for event in events]
        with patch("requests.post", return_value=response):
            result = list(client._anthropic_stream(cfg, [{"role": "user", "content": "weather"}], [TOOL]))
        metadata = next(event["data"] for event in result if event["type"] == "assistant_meta")
        self.assertEqual([event["text"] for event in result if event["type"] == "delta"], ["checking"])
        blocks = metadata["anthropic_content"]
        self.assertEqual([block["type"] for block in blocks], ["thinking", "text", "redacted_thinking", "tool_use"])
        self.assertEqual(blocks[0]["signature"], "sig-1-2")
        self.assertEqual(blocks[2]["data"], "opaque")
        msg = {"role": "assistant", "content": "checking", **metadata}
        body = client._anthropic_body(cfg, [{"role": "user", "content": "weather"}, msg,
            {"role": "tool", "tool_call_id": "t1", "content": "sunny"}], [TOOL], True)
        self.assertEqual(body["messages"][1]["content"], blocks)
        self.assertEqual(body["messages"][2]["content"][0]["type"], "tool_result")
        response.close.assert_called_once()
        # Both production and the existing helper use exactly the same parser.
        helper = iter_anthropic_sse(events)
        self.assertEqual(next(e["data"]["anthropic_content"] for e in helper if e["type"] == "assistant_meta"), blocks)

    def test_anthropic_stream_error_is_not_a_successful_empty_answer(self):
        with self.assertRaisesRegex(LLMError, "overloaded"):
            iter_anthropic_sse([{"type": "error", "error": {"message": "overloaded"}}])

    def test_qwen_open_weight_nonstream_chat_uses_stream_endpoint(self):
        cfg = config(model="qwen3-32b", base="https://dashscope.aliyuncs.com/compatible-mode/v1", level="high")
        client = LLMClient()
        with patch.object(client, "_read_config", return_value=cfg), \
                patch.object(client, "chat_stream", return_value=iter([{"type": "delta", "text": "summary"}])), \
                patch("app.utils.urlsafety.check_provider_url", side_effect=lambda value: value):
            self.assertEqual(client.chat([{"role": "user", "content": "test"}]), ("summary", []))


class ReasoningConfigTests(IsolatedAppTestCase):
    def test_overrides_cannot_change_endpoint_or_secret(self):
        from app.services.settings_service import set_setting
        from app.utils.scoping import set_current_user_id

        user = self.make_user()
        set_current_user_id(user.id)
        set_setting("llm_api_key", "owner-secret", user_id=user.id)
        client = LLMClient(overrides={"base_url": "https://attacker.invalid", "api_key": "other", "model": "qwen-plus", "reasoning_effort": "default"})
        cfg = client._read_config()
        self.assertEqual(cfg["api_key"], "owner-secret")
        self.assertNotEqual(cfg["base_url"], "https://attacker.invalid")
        self.assertEqual(cfg["model"], "qwen-plus")


if __name__ == "__main__":
    unittest.main()
