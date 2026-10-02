"""Private saved model/persona profiles and cross-channel selection contracts."""
import json
from unittest.mock import Mock, patch

from scripts.tests.support import IsolatedAppTestCase
from app.ai.llm import LLMClient
from app.ai.prompts import build_system_prompt
from app.extensions import db
from app.models.conversation import Conversation
from app.services import model_control_service as controls
from app.services import profile_service as profiles
from app.services.settings_service import get_own_setting, set_setting
from app.utils.scoping import clear_current_user_id, set_current_user_id


class ModelProfileTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        set_current_user_id(self.user.id)
        self.conv = Conversation(user_id=self.user.id, title="profile selection")
        self.other_conv = Conversation(user_id=self.user.id, title="unaffected")
        db.session.add_all([self.conv, self.other_conv])
        db.session.commit()
        self.app.config["WTF_CSRF_ENABLED"] = False

    def config(self, kind, suffix="first"):
        if kind == "prompt":
            return {"name": f"助手{suffix}", "preset": "custom", "verbosity": "concise",
                    "address": "朋友", "extra": f"PROMPT_PRIVATE_{suffix}",
                    "ack_template": "收到 {message}", "ack_enabled": True}
        config = {"protocol": "openai", "base_url": f"https://{suffix}.example/v1",
                  "model": f"{suffix}-model", "api_key": f"PRIVATE_KEY_{kind}_{suffix}",
                  "timeout": 90, "reasoning_effort": "default"}
        if kind == "chat":
            config.update(models=[f"{suffix}-model", "alternative-model"],
                          context_window_tokens=1000000, vision_capability="on")
        elif kind == "image":
            config["size"] = "1024x1024"
        return config

    def save(self, kind="chat", suffix="first", **kwargs):
        return profiles.save_profile(kind, f"配置{suffix}", self.config(kind, suffix), **kwargs)

    def test_four_catalogs_save_activate_and_leave_legacy_settings_untouched(self):
        set_setting("llm_api_key", "LEGACY_PRIVATE_KEY")
        set_setting("llm_model", "legacy-model")
        for kind in profiles.KINDS:
            with self.subTest(kind=kind):
                first = self.save(kind)
                second = self.save(kind, "second")
                self.assertNotEqual(first, second)
                self.assertEqual(profiles.resolve_profile_config(kind)["profile_id"], second)
                profiles.activate_profile(kind, first)
                resolved = profiles.resolve_profile_config(kind)
                self.assertEqual(resolved["profile_id"], first)
                self.assertEqual(resolved["profile_name"], "配置first")
                self.assertEqual(profiles.profiles_view()[kind]["active_id"], first)
                profiles.activate_profile(kind, "legacy")
                self.assertEqual(profiles.resolve_profile_config(kind)["profile_id"], "legacy")
        self.assertEqual(get_own_setting("llm_api_key"), "LEGACY_PRIVATE_KEY")
        self.assertEqual(get_own_setting("llm_model"), "legacy-model")

    def test_catalog_views_and_chat_controls_never_contain_saved_keys(self):
        for kind in profiles.KINDS:
            self.save(kind)
        public = json.dumps({"views": profiles.profiles_view(),
                             "choices": {kind: profiles.profile_choices(kind) for kind in profiles.KINDS},
                             "controls": controls.chat_controls(self.conv, self.user)})
        self.assertNotIn("api_key", public)
        self.assertNotIn("PRIVATE_KEY_", public)
        self.assertNotIn("PROMPT_PRIVATE_", public)
        self.assertNotIn("base_url", public)

    def test_edit_preserves_key_only_for_same_endpoint_and_protocol(self):
        for kind in ("chat", "image", "vision"):
            with self.subTest(kind=kind):
                chosen = self.save(kind)
                config = self.config(kind)
                config["api_key"] = ""
                config["model"] = "edited-model"
                self.assertEqual(profiles.save_profile(kind, "改名", config, profile_id=chosen), chosen)
                self.assertEqual(profiles.resolve_profile_config(kind)["api_key"], f"PRIVATE_KEY_{kind}_first")
                for field, value in (("base_url", "https://different.example/v1"),
                                     ("protocol", "anthropic")):
                    with self.assertRaisesRegex(ValueError, "API Key"):
                        profiles.save_profile(kind, "拒绝换接口", {**config, field: value}, profile_id=chosen)
                    self.assertEqual(profiles.resolve_profile_config(kind)[field], config[field])
                profiles.save_profile(kind, "主动清除", config, profile_id=chosen, clear_key=True)
                self.assertEqual(profiles.resolve_profile_config(kind)["api_key"], "")

    def test_save_as_new_copies_config_without_overwriting_original(self):
        first = self.save()
        config = self.config("chat")
        config.update(api_key="", model="copied-model")
        second = profiles.save_profile("chat", "副本", config, profile_id=first, save_as_new=True)
        self.assertNotEqual(first, second)
        original = profiles.resolve_profile_config("chat", profile_id=first)
        duplicate = profiles.resolve_profile_config("chat", profile_id=second)
        self.assertEqual(original["model"], "first-model")
        self.assertEqual(duplicate["model"], "copied-model")
        self.assertEqual(duplicate["api_key"], original["api_key"])
        self.assertEqual(len(profiles.profile_choices("chat")), 3)

    def test_tenants_cannot_activate_edit_delete_or_use_foreign_saved_profile(self):
        ids = {kind: self.save(kind) for kind in profiles.KINDS}
        stranger = self.make_user("profile-stranger", False)
        set_current_user_id(stranger.id)
        for kind, foreign_id in ids.items():
            with self.subTest(kind=kind):
                self.assertEqual(len(profiles.profile_choices(kind)), 1)
                self.assertNotIn("PRIVATE_KEY_", json.dumps(profiles.resolve_profile_config(kind, profile_id=foreign_id)))
                for mutation in (
                    lambda: profiles.activate_profile(kind, foreign_id),
                    lambda: profiles.delete_profile(kind, foreign_id),
                    lambda: profiles.save_profile(kind, "foreign", self.config(kind), profile_id=foreign_id),
                ):
                    with self.assertRaises(ValueError):
                        mutation()
                with self.assertRaises(ValueError):
                    profiles.resolve_profile_config(kind, conversation=self.conv)
        with self.assertRaises(ValueError):
            controls.update_chat_profile(self.conv, stranger, "chat", ids["chat"])

    def test_deleted_conversation_profile_falls_back_to_own_default(self):
        first = self.save()
        second = self.save(suffix="second")
        controls.update_chat_profile(self.conv, self.user, "chat", first)
        profiles.delete_profile("chat", first)
        resolved = LLMClient(conversation=self.conv)._read_config()
        self.assertEqual(resolved["profile_id"], second)
        self.assertEqual(resolved["api_key"], "PRIVATE_KEY_chat_second")
        profiles.delete_profile("chat", second)
        self.assertEqual(LLMClient(conversation=self.conv)._read_config()["profile_id"], "legacy")
        self.assertEqual(LLMClient(conversation=self.conv)._read_config()["api_key"], "")
        with self.assertRaises(ValueError):
            profiles.delete_profile("chat", "legacy")

    def test_conversation_switch_changes_endpoint_protocol_key_and_capacity_together(self):
        first = self.save()
        config = self.config("chat", "second")
        config.update(protocol="anthropic", model="claude-model", context_window_tokens=2000000,
                      models=["claude-model"], vision_capability="off")
        second = profiles.save_profile("chat", "Claude配置", config)
        controls.update_chat_profile(self.conv, self.user, "chat", first)
        first_cfg = LLMClient(conversation=self.conv)._read_config()
        self.assertEqual(first_cfg["protocol"], "openai")
        self.assertEqual(first_cfg["api_key"], "PRIVATE_KEY_chat_first")
        controls.update_chat_profile(self.conv, self.user, "chat", second)
        selected = LLMClient(conversation=self.conv)._read_config()
        for key in ("protocol", "base_url", "api_key", "model", "context_window_tokens", "vision_capability"):
            self.assertEqual(selected[key], config[key])
        profiles.activate_profile("chat", first)
        self.assertEqual(LLMClient(conversation=self.conv)._read_config()["profile_id"], second)
        self.assertEqual(LLMClient(conversation=self.other_conv)._read_config()["profile_id"], first)

    def test_legacy_model_override_does_not_pollute_other_provider_profile(self):
        set_setting("llm_model", "legacy-model")
        set_setting("llm_models", ["legacy-model", "alternative-model"])
        controls.update_chat_model(self.conv, self.user, model="alternative-model")
        chosen = self.save()
        selected = LLMClient(conversation=self.conv)._read_config()
        self.assertEqual(selected["profile_id"], chosen)
        self.assertEqual(selected["model"], "first-model")
        controls.update_chat_model(self.conv, self.user, model="alternative-model")
        second = self.save(suffix="second")
        self.assertEqual(LLMClient(conversation=self.conv)._read_config()["profile_id"], second)
        self.assertEqual(LLMClient(conversation=self.conv)._read_config()["model"], "second-model")

    def test_candidate_model_does_not_inherit_primary_capacity_or_vision_declaration(self):
        chosen = self.save()
        before = LLMClient(conversation=self.conv)._read_config()
        self.assertEqual(before["context_window_tokens"], 1000000)
        self.assertEqual(before["vision_capability"], "on")
        controls.update_chat_model(self.conv, self.user, model="alternative-model")
        candidate = LLMClient(conversation=self.conv)._read_config()
        self.assertEqual(candidate["model"], "alternative-model")
        self.assertEqual(candidate["context_window_tokens"], 0)
        self.assertEqual(candidate["vision_capability"], "auto")
        for key in ("profile_id", "protocol", "base_url", "api_key"):
            self.assertEqual(candidate[key], before[key])
        # Runtime clearing is a resolved view, not a mutation of the saved group.
        stored = profiles.resolve_profile_config("chat", profile_id=chosen)
        self.assertEqual(stored["context_window_tokens"], 1000000)
        self.assertEqual(stored["vision_capability"], "on")
        controls.update_chat_model(self.conv, self.user, model="first-model")
        restored = LLMClient(conversation=self.conv)._read_config()
        self.assertEqual(restored["context_window_tokens"], 1000000)
        self.assertEqual(restored["vision_capability"], "on")

    def test_editing_profile_primary_model_does_not_apply_new_capabilities_to_old_override(self):
        chosen = self.save()
        controls.update_chat_model(self.conv, self.user, model="first-model")
        edited = {**self.config("chat"), "model": "replacement-model",
                  "models": ["replacement-model", "first-model"],
                  "context_window_tokens": 2000000, "vision_capability": "off"}
        profiles.save_profile("chat", "原地改主模型", edited, profile_id=chosen)
        retained_override = LLMClient(conversation=self.conv)._read_config()
        self.assertEqual(retained_override["model"], "first-model")
        self.assertEqual(retained_override["context_window_tokens"], 0)
        self.assertEqual(retained_override["vision_capability"], "auto")
        controls.update_chat_model(self.conv, self.user, model="replacement-model")
        new_primary = LLMClient(conversation=self.conv)._read_config()
        self.assertEqual(new_primary["context_window_tokens"], 2000000)
        self.assertEqual(new_primary["vision_capability"], "off")
        # Removing an old override from candidates falls back to the newly
        # selected primary, where its own declarations correctly apply.
        controls.update_chat_model(self.conv, self.user, model="first-model")
        profiles.save_profile("chat", "移除旧候选", {**edited, "models": ["replacement-model"]},
                              profile_id=chosen)
        fallback = LLMClient(conversation=self.conv)._read_config()
        self.assertEqual(fallback["model"], "replacement-model")
        self.assertEqual(fallback["context_window_tokens"], 2000000)
        self.assertEqual(fallback["vision_capability"], "off")

    def test_native_vision_auto_recognizes_verified_families_and_respects_explicit_modes(self):
        from app.services.vision_capabilities import supports_native_vision

        recognized = (
            "gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-5-2025-08-07",
            "gpt-5.1", "gpt-5.1-2025-11-13", "openai/gpt-5.1",
            "qwen-vl-max", "qwen-vl-plus-latest", "qwen-vl-max-2025-01-25",
            "qwen3-vl-plus", "qwen3-vl-flash-latest",
            "Qwen/Qwen2.5-VL-72B-Instruct", "Qwen/Qwen3-VL-235B-A22B-Thinking",
        )
        for model in recognized:
            with self.subTest(model=model):
                self.assertTrue(supports_native_vision({"model": model, "vision_capability": "auto"}))
                self.assertFalse(supports_native_vision({"model": model, "vision_capability": "off"}))
        for model in ("", "unknown-private-model", "gpt-5-unverified-vision", "qwen3-custom-vl-alias"):
            with self.subTest(model=model):
                self.assertFalse(supports_native_vision({"model": model}))
                self.assertTrue(supports_native_vision({"model": model, "vision_capability": "on"}))

    def test_manual_lock_blocks_ai_and_two_switch_limit_is_shared_between_kinds(self):
        first = self.save()
        second = self.save(suffix="second")
        prompt = self.save("prompt")
        controls.update_chat_profile(self.conv, self.user, "chat", first)
        with controls.bind_conversation(self.conv, self.user):
            with self.assertRaisesRegex(ValueError, "锁定"):
                controls.update_chat_profile(self.conv, self.user, "prompt", prompt, by_ai=True)
        controls.set_auto_switch(self.conv, self.user, True)
        with controls.bind_conversation(self.conv, self.user):
            controls.update_chat_profile(self.conv, self.user, "chat", second, by_ai=True)
            controls.update_chat_profile(self.conv, self.user, "prompt", prompt, by_ai=True)
            with self.assertRaisesRegex(ValueError, "两次"):
                controls.update_chat_model(self.conv, self.user, model="alternative-model", by_ai=True)
        with controls.bind_conversation(self.conv, self.user):
            controls.update_chat_profile(self.conv, self.user, "chat", first, by_ai=True)
        self.assertEqual(LLMClient(conversation=self.conv)._read_config()["profile_id"], first)

    def test_prompt_selection_affects_system_prompt_and_bound_conversation_only(self):
        first = self.save("prompt")
        second = self.save("prompt", "second")
        controls.update_chat_profile(self.conv, self.user, "prompt", first)
        selected = build_system_prompt(self.user, conversation=self.conv)
        self.assertIn("PROMPT_PRIVATE_first", selected)
        self.assertNotIn("PROMPT_PRIVATE_second", selected)
        self.assertIn("PROMPT_PRIVATE_second", build_system_prompt(self.user, conversation=self.other_conv))
        with controls.bind_conversation(self.conv, self.user):
            self.assertIn("PROMPT_PRIVATE_first", build_system_prompt(self.user))
        self.assertIn("PROMPT_PRIVATE_second", build_system_prompt(self.user))
        self.assertEqual(profiles.resolve_profile_config("prompt")["profile_id"], second)

    def test_image_and_vision_services_use_bound_selection_and_restore_defaults(self):
        from app.services import image_service, vision_service

        for kind, read in (("image", image_service._read_config), ("vision", vision_service._read_config)):
            with self.subTest(kind=kind):
                first = self.save(kind)
                second = self.save(kind, "second")
                controls.update_chat_profile(self.conv, self.user, kind, first)
                self.assertEqual(read()["profile_id"], second)
                with controls.bind_conversation(self.conv, self.user):
                    self.assertEqual(read()["profile_id"], first)
                    self.assertEqual(read()["api_key"], f"PRIVATE_KEY_{kind}_first")
                self.assertEqual(read()["profile_id"], second)

    def test_invalid_profiles_fail_atomically_and_custom_capacity_is_preserved(self):
        first = self.save()
        initial = get_own_setting("model_profiles:chat")
        cases = ({"base_url": "https://user:password@invalid.example/v1"},
                 {"base_url": "https://invalid.example/v1?secret=x"},
                 {"base_url": "https://invalid.example:999999/v1"},
                 {"model": "bad model"}, {"protocol": "unsupported"},
                 {"vision_capability": "maybe"}, {"context_window_tokens": -1},
                 {"context_window_tokens": 8192}, {"context_window_tokens": 31999},
                 {"context_window_tokens": 10000001})
        for update in cases:
            with self.subTest(update=update):
                with self.assertRaises(ValueError):
                    profiles.save_profile("chat", "invalid", {**self.config("chat"), **update}, profile_id=first)
                self.assertEqual(get_own_setting("model_profiles:chat"), initial)
        profiles.save_profile("chat", "自定义容量", {**self.config("chat"), "context_window_tokens": 345678}, profile_id=first)
        self.assertEqual(profiles.resolve_profile_config("chat")["context_window_tokens"], 345678)

    def test_slash_commands_switch_all_profile_types_without_calling_a_model(self):
        from app.ai.executor import run_chat

        commands = {"chat": "/profile", "prompt": "/prompt", "image": "/image", "vision": "/vision"}
        for kind, command in commands.items():
            chosen = self.save(kind)
            profiles.activate_profile(kind, "legacy")
            with patch("app.ai.llm.LLMClient.chat", side_effect=AssertionError("No LLM call expected")), \
                    patch("app.ai.llm.LLMClient.chat_stream", side_effect=AssertionError("No LLM call expected")):
                events = list(run_chat(self.conv, command + " 2", self.user))
            self.assertFalse([item for item in events if item[0] == "error"], events)
            self.assertEqual(profiles.resolve_profile_config(kind, conversation=self.conv)["profile_id"], chosen)
            self.assertEqual(profiles.resolve_profile_config(kind, conversation=self.other_conv)["profile_id"], "legacy")

    def test_ai_whole_profile_and_prompt_switch_rebuilds_same_turn_and_next_turn(self):
        from app.ai.executor import run_chat
        from app.models.conversation import Message

        first = self.save()
        other = self.config("chat", "second")
        other["protocol"] = "anthropic"
        second = profiles.save_profile("chat", "另一协议", other)
        profiles.activate_profile("chat", first)
        prompt_first = self.save("prompt")
        prompt_second = self.save("prompt", "second")
        profiles.activate_profile("prompt", prompt_first)
        requests = []

        def stream(client, messages, tools=None):
            requests.append((client._read_config(), json.loads(json.dumps(messages))))
            if len(requests) == 1:
                yield {"type": "delta", "text": "COMPLETE_PLAN"}
                yield {"type": "assistant_meta", "data": {
                    "reasoning_content": "PRIVATE_PROVIDER_REASONING",
                    "_llm_signature": ["openai", "old-signature"],
                }}
                yield {"type": "tool_calls", "calls": [
                    {"id": "switch-model", "name": "switch_chat_profile", "arguments": json.dumps({
                        "kind": "chat", "profile_id": second, "reason": "needs another provider"})},
                    {"id": "switch-prompt", "name": "switch_chat_profile", "arguments": json.dumps({
                        "kind": "prompt", "profile_id": prompt_second, "reason": "needs another persona"})},
                ]}
            else:
                yield {"type": "delta", "text": "已完成"}

        with self.app.test_request_context("/"), patch.object(LLMClient, "chat_stream", stream), \
                patch("app.ai.executor.ack_received", return_value=""):
            from flask_login import login_user

            login_user(self.user)
            events = list(run_chat(self.conv, "使用适合的配置处理", self.user))
            following_events = list(run_chat(self.conv, "继续上一轮", self.user))
        self.assertFalse([event for event in events + following_events if event[0] == "error"], events)
        self.assertEqual(len(requests), 3)
        for cfg, messages in requests[1:]:
            for key in ("protocol", "base_url", "api_key", "model", "context_window_tokens"):
                self.assertEqual(cfg[key], other[key])
            systems = "\n".join(item["content"] for item in messages if item["role"] == "system")
            self.assertIn("PROMPT_PRIVATE_second", systems)
            self.assertNotIn("PROMPT_PRIVATE_first", systems)
            self.assertNotIn("PRIVATE_PROVIDER_REASONING", json.dumps(messages))
        rebuilt = requests[1][1]
        self.assertFalse(any(item.get("tool_calls") or item["role"] == "tool" for item in rebuilt))
        self.assertIn("COMPLETE_PLAN", json.dumps(rebuilt))
        self.assertIn("switch_chat_profile", json.dumps(rebuilt))
        self.assertNotIn("PRIVATE_PROVIDER_REASONING", json.dumps(events))
        self.assertNotIn("PRIVATE_KEY_", json.dumps(events))
        self.assertNotIn("PRIVATE_PROVIDER_REASONING", "\n".join(row.content for row in Message.query.all()))

    def test_image_generation_retries_and_download_keep_same_config_snapshot(self):
        from app.services import image_service

        first, second = self.config("image"), self.config("image", "second")
        rejected = Mock(status_code=400, text="b64_json unsupported")
        accepted = Mock(status_code=200, text="ok")
        accepted.json.return_value = {"data": [{"url": "/result.png"}]}
        downloaded = Mock(status_code=200, headers={"Content-Type": "image/png"}, content=b"PNG_BYTES")
        with patch.object(image_service, "_read_config", side_effect=[first, second]) as read, \
                patch.object(image_service, "_safe_base", side_effect=lambda cfg: cfg["base_url"]), \
                patch.object(image_service.requests, "post", side_effect=[rejected, accepted]) as post, \
                patch("app.utils.urlsafety.validate_public_url", side_effect=lambda url: url), \
                patch("app.utils.urlsafety.safe_get", return_value=downloaded) as get:
            result = image_service._generate_bytes("picture", "1024x1024", first["model"])
        self.assertEqual(result, b"PNG_BYTES")
        self.assertEqual(read.call_count, 1)
        self.assertEqual(post.call_count, 2)
        for call in post.call_args_list:
            self.assertEqual(call.args[0], first["base_url"] + "/images/generations")
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer " + first["api_key"])
            self.assertEqual(call.kwargs["json"]["model"], first["model"])
        self.assertEqual(get.call_args.args[0], first["base_url"] + "/result.png")

    def _form(self, kind, suffix="first"):
        cfg = self.config(kind, suffix)
        prefix = "llm_" if kind == "chat" else "ai_persona_" if kind == "prompt" else kind + "_"
        data = {prefix + key: value for key, value in cfg.items() if key not in ("timeout", "models")}
        if kind == "chat":
            data["llm_models"] = "\n".join(cfg["models"])
        if kind == "prompt":
            data["ai_persona_ack_enabled"] = "1"
        data.update(profile_name=f"HTTP配置{suffix}", profile_id="legacy", save_as_new="1")
        return data

    def test_http_save_activate_delete_for_each_profile_type(self):
        clear_current_user_id()
        client = self.client_for(self.user)
        for kind in profiles.KINDS:
            with self.subTest(kind=kind):
                response = client.post(f"/settings/profiles/{kind}/save", data=self._form(kind))
                self.assertEqual(response.status_code, 302)
                first = profiles.resolve_profile_config(kind, user_id=self.user.id)["profile_id"]
                self.assertNotEqual(first, "legacy")
                response = client.post(f"/settings/profiles/{kind}/save", data=self._form(kind, "second"))
                self.assertEqual(response.status_code, 302)
                second = profiles.resolve_profile_config(kind, user_id=self.user.id)["profile_id"]
                self.assertNotEqual(first, second)
                self.assertEqual(client.post(f"/settings/profiles/{kind}/activate", data={"profile_id": first}).status_code, 302)
                self.assertEqual(profiles.resolve_profile_config(kind, user_id=self.user.id)["profile_id"], first)
                self.assertEqual(client.post(f"/settings/profiles/{kind}/delete", data={"profile_id": first}).status_code, 302)
                self.assertNotIn(first, [item["id"] for item in profiles.profile_choices(kind, self.user.id)])

    def test_settings_render_all_profile_controls_without_saved_secrets(self):
        for kind in profiles.KINDS:
            self.save(kind)
        clear_current_user_id()
        response = self.client_for(self.user).get("/settings/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn("PRIVATE_KEY_", html)
        for kind in profiles.KINDS:
            self.assertIn(f"/settings/profiles/{kind}/save", html)
            self.assertIn(f"/settings/profiles/{kind}/activate", html)

    def test_http_foreign_ids_and_anonymous_requests_cannot_change_catalogs(self):
        foreign = self.save()
        stranger = self.make_user("http-stranger", False)
        clear_current_user_id()
        client = self.client_for(stranger)
        for action in ("activate", "delete"):
            response = client.post(f"/settings/profiles/chat/{action}", data={"profile_id": foreign})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(profiles.resolve_profile_config("chat", user_id=self.user.id)["profile_id"], foreign)
            self.assertEqual(profiles.resolve_profile_config("chat", user_id=stranger.id)["profile_id"], "legacy")
        data = self._form("chat")
        data["profile_id"] = foreign
        client.post("/settings/profiles/chat/save", data=data)
        self.assertEqual(len(profiles.profile_choices("chat", stranger.id)), 1)
        response = self.app.test_client().post("/settings/profiles/chat/save", data=self._form("chat"))
        self.assertIn(response.status_code, (302, 401))
        self.assertEqual(len(profiles.profile_choices("chat", self.user.id)), 2)


if __name__ == "__main__":
    import unittest
    unittest.main()
