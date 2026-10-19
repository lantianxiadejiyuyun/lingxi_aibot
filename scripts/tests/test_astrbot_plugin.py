"""Offline protocol/transport checks; no AstrBot installation or real messages."""
from __future__ import annotations

import asyncio
import copy
import importlib
import json
import logging
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from integrations.astrbot.bridge import (
    Account, Bridge, BridgeError, CHUNK_SIZE, Client, parse_accounts, private_session,
)


SESSION = "telegram_main:FriendMessage:123"
ACCOUNT = Account("test", "http://lingxi.test/lingxi/api/v1", "lx_test_only", SESSION)
BINDING = {"bound": True, "session": SESSION, "conversation_id": 17}
LEASE_ONE = "a" * 48
LEASE_TWO = "b" * 48


class Store:
    def __init__(self):
        self.value = {}

    async def load(self):
        return copy.deepcopy(self.value)

    async def save(self, value):
        self.value = json.loads(json.dumps(value))


class FakeClient:
    account = ACCOUNT

    def __init__(self):
        self.binding = AsyncMock(return_value=BINDING)
        self.request = AsyncMock(return_value={"reply": "done"})
        self.bind = AsyncMock(return_value=BINDING)
        self.unbind = AsyncMock()


def queue_item(text="hello", ident=1, lease=LEASE_ONE, session=SESSION):
    return {"id": ident, "lease_token": lease, "session": session, "text": text}


class ConfigTests(unittest.TestCase):
    def config(self, **changes):
        row = {"api_base": ACCOUNT.api_base, "api_token": ACCOUNT.api_token,
               "private_session": SESSION}
        row.update(changes)
        return {"accounts": [row]}

    def test_exact_private_sessions_only(self):
        self.assertTrue(private_session(SESSION))
        self.assertTrue(private_session("p:FriendMessage:user:subchannel"))
        for value in ("p:GroupMessage:123", "p:OtherMessage:123", "p:FriendMessage:",
                      ":FriendMessage:123", SESSION + "\n", " " + SESSION, SESSION + " ", None):
            self.assertFalse(private_session(value))

    def test_url_credentials_redirect_targets_and_bad_tokens_rejected(self):
        for url in ("https://user:pass@example.test/api/v1", "https://e.test/api/v1?token=x",
                    "file:///api/v1", "https://e.test/api/v1#x", "https://e.test",
                    "https://e.test:bad/api/v1"):
            with self.assertRaises(ValueError):
                parse_accounts(self.config(api_base=url))
        with self.assertRaises(ValueError):
            parse_accounts(self.config(api_token="secret\nheader"))

    def test_duplicate_accounts_and_private_routes_rejected(self):
        config = self.config()
        config["accounts"].append(dict(config["accounts"][0]))
        with self.assertRaises(ValueError):
            parse_accounts(config)
        config["accounts"][1]["private_session"] = "other:FriendMessage:456"
        with self.assertRaises(ValueError):
            parse_accounts(config)

    def test_account_state_scopes_include_token_without_exposing_it(self):
        account = parse_accounts(self.config())[0]
        other = parse_accounts(self.config(api_token="rotated"))[0]
        self.assertNotEqual(account.storage_key, other.storage_key)
        self.assertNotIn(ACCOUNT.api_token, repr(account))
        self.assertNotIn(ACCOUNT.api_token, account.storage_key)

    def test_disabled_rows_do_not_need_credentials(self):
        self.assertEqual(parse_accounts({"accounts": [{"enabled": False}]}), [])


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FakeClient()
        self.store = Store()
        self.bridge = Bridge(self.client, self.store.load, self.store.save)
        self.send = AsyncMock(return_value=True)

    async def test_chat_uses_account_bound_conversation_and_dedupes_restart(self):
        self.assertEqual(await self.bridge.chat("tomorrow's tasks", "m-1", self.send), "sent")
        self.assertEqual(self.client.request.call_args.args[2]["conversation_id"], 17)
        other = Bridge(self.client, self.store.load, self.store.save)
        self.assertEqual(await other.chat("tomorrow's tasks", "m-1", self.send), "duplicate")
        self.client.request.assert_awaited_once()
        self.send.assert_awaited_once_with("done")
        self.assertNotIn("tomorrow", str(self.store.value))

    async def test_concurrent_redelivery_runs_once(self):
        await asyncio.gather(self.bridge.chat("create task", "1", self.send),
                             self.bridge.chat("create task", "1", self.send))
        self.client.request.assert_awaited_once()

    async def test_failed_or_interrupted_chat_never_automatically_replays(self):
        self.client.request.side_effect = BridgeError()
        with self.assertRaises(BridgeError):
            await self.bridge.chat("create task", "1", self.send)
        self.assertEqual(await self.bridge.chat("create task", "1", self.send), "duplicate")
        self.client.request.assert_awaited_once()
        self.send.assert_not_awaited()

    async def test_pre_request_durable_write_failure_prevents_execution(self):
        self.bridge.save = AsyncMock(side_effect=OSError("disk unavailable"))
        with self.assertRaises(OSError):
            await self.bridge.chat("create task", "1", self.send)
        self.client.request.assert_not_awaited()

    async def test_chat_unbound_does_not_execute(self):
        self.client.binding.return_value = None
        with self.assertRaisesRegex(BridgeError, "not_bound"):
            await self.bridge.chat("hello", "1", self.send)
        self.client.request.assert_not_awaited()

    async def test_deleted_conversation_needs_explicit_rebind(self):
        self.client.binding.return_value = {**BINDING, "conversation_id": None}
        with self.assertRaisesRegex(BridgeError, "conversation_missing"):
            await self.bridge.chat("hello", "1", self.send)
        self.client.request.assert_not_awaited()

    async def test_chat_reply_chunking_is_complete(self):
        text = "a" * 5001
        self.client.request.return_value = {"reply": text}
        await self.bridge.chat("hello", "1", self.send)
        self.assertEqual("".join(c.args[0] for c in self.send.call_args_list), text)
        self.assertTrue(all(len(c.args[0]) <= CHUNK_SIZE for c in self.send.call_args_list))

    async def test_poll_sends_only_verified_private_binding_and_acks(self):
        self.client.request.side_effect = [[queue_item()], {}]
        await self.bridge.poll(self.send)
        self.send.assert_awaited_once_with(SESSION, "hello")
        self.assertEqual(self.client.request.call_args.args,
                         ("POST", "/integrations/astrbot/outbox/1/ack",
                          {"lease_token": LEASE_ONE, "outcome": "sent"}))

    async def test_wrong_recipient_never_receives_private_data(self):
        for session in ("other:FriendMessage:456", "telegram_main:GroupMessage:123"):
            self.client.request.side_effect = [[queue_item(session=session)], {}]
            await self.bridge.poll(self.send)
            self.assertEqual(self.client.request.call_args.args[2]["outcome"], "failed")
        self.send.assert_not_awaited()

    async def test_missing_platform_is_failure_not_sent(self):
        self.send.return_value = False
        self.client.request.side_effect = [[queue_item()], {}]
        await self.bridge.poll(self.send)
        self.assertEqual(self.client.request.call_args.args[2]["outcome"], "failed")
        self.assertFalse(self.store.value.get("outgoing"))

    async def test_ack_loss_reclaims_without_resending_completed_message(self):
        self.client.request.side_effect = [[queue_item()], BridgeError()]
        with self.assertRaises(BridgeError):
            await self.bridge.poll(self.send)
        restarted = Bridge(self.client, self.store.load, self.store.save)
        self.client.request.side_effect = [[queue_item(lease=LEASE_TWO)], {}]
        await restarted.poll(self.send)
        self.send.assert_awaited_once()
        self.assertEqual(self.client.request.call_args.args[2]["lease_token"], LEASE_TWO)

    async def test_partial_message_resumes_from_last_saved_chunk(self):
        text = "a" * 12000
        self.client.request.side_effect = [[queue_item(text)], {}]
        self.send.side_effect = [True, RuntimeError("platform failed")]
        await self.bridge.poll(self.send)
        self.assertEqual(self.store.value["outgoing"]["1"]["chunks"], 1)
        self.assertEqual(self.client.request.call_args.args[2]["outcome"], "failed")
        self.send.reset_mock(side_effect=True)
        self.send.return_value = True
        self.client.request.side_effect = [[queue_item(text, lease=LEASE_TWO)], {}]
        await self.bridge.poll(self.send)
        self.assertEqual("".join(c.args[1] for c in self.send.call_args_list), text[CHUNK_SIZE:])
        self.assertEqual(self.client.request.call_args.args[2]["outcome"], "sent")

    async def test_changed_body_for_same_queue_id_is_not_sent(self):
        self.client.request.side_effect = [[queue_item()], {}]
        await self.bridge.poll(self.send)
        self.send.reset_mock()
        self.client.request.side_effect = [[queue_item("different")], {}]
        await self.bridge.poll(self.send)
        self.send.assert_not_awaited()
        self.assertEqual(self.client.request.call_args.args[2]["outcome"], "failed")

    async def test_unbound_account_is_not_polled(self):
        self.client.binding.return_value = None
        await self.bridge.poll(self.send)
        self.client.request.assert_not_awaited()

    async def test_oversized_or_empty_outbox_is_failed_without_truncation(self):
        for text in ("x" * 12001, "   "):
            self.client.request.side_effect = [[queue_item(text)], {}]
            await self.bridge.poll(self.send)
        self.send.assert_not_awaited()


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_header_and_redirect_disabled(self):
        http = types.SimpleNamespace(request=AsyncMock())
        http.request.return_value = types.SimpleNamespace(
            status_code=200, json=lambda: {"ok": True, "data": BINDING})
        client = Client(ACCOUNT, http)
        await client.binding()
        kwargs = http.request.call_args.kwargs
        self.assertFalse(kwargs["follow_redirects"])
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer lx_test_only")

    async def test_exception_and_server_content_never_leak_secrets(self):
        http = types.SimpleNamespace(request=AsyncMock(side_effect=RuntimeError("secret-token")))
        with self.assertRaises(BridgeError) as caught:
            await Client(ACCOUNT, http).binding()
        self.assertNotIn("secret-token", str(caught.exception))
        http.request.side_effect = None
        http.request.return_value = types.SimpleNamespace(
            status_code=401, json=lambda: {"ok": False, "error": "secret-token"})
        with self.assertRaisesRegex(BridgeError, "unauthorized"):
            await Client(ACCOUNT, http).binding()

    async def test_binding_mismatch_cannot_unbind_another_recipient(self):
        http = types.SimpleNamespace(request=AsyncMock())
        http.request.return_value = types.SimpleNamespace(
            status_code=200, json=lambda: {"ok": True, "data": {**BINDING, "session": "other"}})
        with self.assertRaisesRegex(BridgeError, "binding_mismatch"):
            await Client(ACCOUNT, http).unbind()
        http.request.assert_awaited_once()

    async def test_deleted_conversation_can_still_be_unbound(self):
        http = types.SimpleNamespace(request=AsyncMock())
        http.request.return_value = types.SimpleNamespace(
            status_code=200, json=lambda: {"ok": True, "data": {**BINDING, "conversation_id": None}})
        await Client(ACCOUNT, http).unbind()
        self.assertEqual(http.request.call_args.args[0], "DELETE")
        self.assertEqual(http.request.call_args.kwargs["json"], {"session": SESSION})


def load_plugin_with_sdk_stub():
    modules = {name: types.ModuleType(name) for name in
               ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star")}
    modules["astrbot.api"].AstrBotConfig = dict
    event = modules["astrbot.api.event"]
    event.AstrMessageEvent = object
    class MessageChain:
        def message(self, text):
            self.text = text
            return self

        def use_t2i(self, value):
            return self

    event.MessageChain = MessageChain
    event.filter = types.SimpleNamespace(
        EventMessageType=types.SimpleNamespace(PRIVATE_MESSAGE="private"),
        event_message_type=lambda *args, **kwargs: lambda fn: fn)
    star = modules["astrbot.api.star"]
    class Star:
        def __init__(self, context):
            self.context = context
            self.logger = logging.getLogger("test_astrbot_bridge")

    star.Context = object
    star.Star = Star
    with patch.dict(sys.modules, modules):
        module = importlib.import_module("integrations.astrbot.main")
    return module.LingxiBridgePlugin


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        plugin_class = load_plugin_with_sdk_stub()
        self.plugin = object.__new__(plugin_class)
        self.plugin.config = {"enabled": True}
        self.plugin.logger = logging.getLogger("test_astrbot_bridge")
        self.client = FakeClient()
        self.store = Store()
        self.bridge = Bridge(self.client, self.store.load, self.store.save)
        self.plugin.bridges = {SESSION: self.bridge}

    def event(self, text="hello", session=SESSION, private=True, components=None):
        value = types.SimpleNamespace(
            unified_msg_origin=session, get_message_str=lambda: text,
            is_private_chat=lambda: private, get_messages=lambda: components or [],
            message_obj=types.SimpleNamespace(message_id="one"),
            send=AsyncMock(), plain_result=lambda body: body, stopped=False)
        value.stop_event = lambda: setattr(value, "stopped", True)
        return value

    async def test_group_is_never_intercepted_even_if_session_matches(self):
        event = self.event(private=False)
        await self.plugin.private_message(event)
        self.assertFalse(event.stopped)
        self.client.binding.assert_not_awaited()

    async def test_unconfigured_private_messages_pass_through(self):
        event = self.event(session="other:FriendMessage:123")
        await self.plugin.private_message(event)
        self.assertFalse(event.stopped)
        event.send.assert_not_awaited()

    async def test_identity_is_available_before_configuration_without_token(self):
        event = self.event("/lingxi 身份", session="new:FriendMessage:123")
        await self.plugin.private_message(event)
        self.assertTrue(event.stopped)
        self.assertIn(event.unified_msg_origin, event.send.call_args.args[0])
        self.assertNotIn(ACCOUNT.api_token, event.send.call_args.args[0])

    async def test_bind_is_explicit_and_allowed_route_only(self):
        denied = self.event("/lingxi 绑定", session="other:FriendMessage:123")
        await self.plugin.private_message(denied)
        self.client.bind.assert_not_awaited()
        event = self.event("/lingxi 绑定")
        await self.plugin.private_message(event)
        self.client.bind.assert_awaited_once()
        self.assertTrue(event.stopped)

    async def test_chat_stops_default_llm_before_http_call(self):
        event = self.event()

        async def request(*args):
            self.assertTrue(event.stopped)
            return {"reply": "response"}

        self.client.request.side_effect = request
        await self.plugin.private_message(event)
        event.send.assert_awaited_once_with("response")

    async def test_failed_chat_remains_stopped(self):
        event = self.event()
        self.client.request.side_effect = BridgeError()
        await self.plugin.private_message(event)
        self.assertTrue(event.stopped)
        self.assertIn("可能已经执行", event.send.call_args.args[0])

    async def test_unsupported_images_do_not_silently_call_text_llm(self):
        image = type("Image", (), {})()
        event = self.event("what is this", components=[image])
        await self.plugin.private_message(event)
        self.client.request.assert_not_awaited()
        self.assertIn("文字", event.send.call_args.args[0])

    async def test_unsupported_platform_never_acknowledged_as_sent(self):
        for name, supported in (("qq_official", True), ("unknown", False)):
            platform = types.SimpleNamespace(meta=lambda: types.SimpleNamespace(
                name=name, support_proactive_message=supported))
            self.plugin.context = types.SimpleNamespace(
                get_platform_inst=lambda ident: platform, send_message=AsyncMock())
            self.assertFalse(await self.plugin._send_proactive(SESSION, "hello"))
            self.plugin.context.send_message.assert_not_awaited()

    async def test_supported_platform_uses_original_private_session(self):
        platform = types.SimpleNamespace(meta=lambda: types.SimpleNamespace(
            name="telegram", support_proactive_message=True))
        self.plugin.context = types.SimpleNamespace(
            get_platform_inst=lambda ident: platform, send_message=AsyncMock(return_value=True))
        self.assertTrue(await self.plugin._send_proactive(SESSION, "hello"))
        self.assertEqual(self.plugin.context.send_message.call_args.args[0], SESSION)

    async def test_empty_accounts_initialization_keeps_identity_command_available(self):
        plugin_class = load_plugin_with_sdk_stub()
        plugin = plugin_class(types.SimpleNamespace(), {"enabled": True, "accounts": []})
        fake_http = types.SimpleNamespace(aclose=AsyncMock(), request=AsyncMock())
        with patch("integrations.astrbot.main.httpx.AsyncClient", return_value=fake_http):
            await plugin.initialize()
        try:
            event = self.event("/lingxi 身份", session="fresh:FriendMessage:456")
            await plugin.private_message(event)
            self.assertTrue(event.stopped)
            self.assertIn("fresh:FriendMessage:456", event.send.call_args.args[0])
            self.assertEqual(plugin.bridges, {})
            fake_http.request.assert_not_awaited()
        finally:
            await plugin.terminate()
        fake_http.aclose.assert_awaited_once()

    async def test_disabled_plugin_does_not_allocate_http_or_poller(self):
        plugin_class = load_plugin_with_sdk_stub()
        plugin = plugin_class(types.SimpleNamespace(), {"enabled": False})
        await plugin.initialize()
        self.assertIsNone(plugin.http)
        self.assertIsNone(plugin.poll_task)
        await plugin.terminate()


if __name__ == "__main__":
    unittest.main()
