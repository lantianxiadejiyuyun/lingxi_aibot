"""Native vision, provider payloads and web/Feishu isolation without live requests."""
import base64
from io import BytesIO
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from scripts.tests.support import IsolatedAppTestCase
from PIL import Image

from app.ai.llm import LLMClient, LLMError, to_anthropic_payload
from app.extensions import db
from app.models.conversation import Conversation, Message
from app.models.image import ImageAsset
from app.services import vision_service
from app.services.settings_service import get_own_setting, set_setting
from app.utils.scoping import current_user_id, set_current_user_id


def image_bytes(fmt="PNG"):
    output = BytesIO()
    Image.new("RGB", (3, 3), "red").save(output, format=fmt)
    return output.getvalue()


def config(model="gpt-4o", protocol="openai"):
    return {"base_url": "https://api.openai.com/v1", "api_key": "sk-isolated-vision",
            "model": model, "protocol": protocol, "timeout": 30, "reasoning_effort": "default"}


def fake_sdk(text="一张红色图片"):
    sdk = Mock()
    sdk.chat.completions.create.return_value = {"choices": [{"message": {"content": text}}]}
    return sdk


class AnthropicImagePayloadTests(unittest.TestCase):
    def test_base64_image_parts_are_converted_without_losing_text(self):
        encoded = base64.b64encode(image_bytes()).decode("ascii")
        result = to_anthropic_payload([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
            {"type": "text", "text": "识别文字"},
        ]}])
        self.assertEqual(result["messages"][0]["content"], [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": encoded}},
            {"type": "text", "text": "识别文字"},
        ])

    def test_https_image_parts_use_native_url_source(self):
        result = to_anthropic_payload([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
        ]}])
        self.assertEqual(result["messages"][0]["content"][0],
                         {"type": "image", "source": {"type": "url", "url": "https://example.com/image.png"}})

    def test_invalid_image_data_fails_before_provider_request(self):
        for url in ("file:///private.png", "data:image/svg+xml;base64,QQ==", "data:image/png;base64,%%%%"):
            with self.subTest(url=url), self.assertRaises(LLMError):
                to_anthropic_payload([{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": url}},
                ]}])


class VisionRoutingTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        set_current_user_id(self.user.id)
        for name, value in {"llm_protocol": "openai", "llm_model": "gpt-4o",
                            "llm_base_url": "https://api.openai.com/v1", "llm_api_key": "sk-user-own"}.items():
            set_setting(name, value)
        self.conv = Conversation(user_id=self.user.id, title="vision")
        db.session.add(self.conv)
        db.session.commit()
        checker = patch("app.utils.urlsafety.check_provider_url", side_effect=lambda value: value)
        checker.start()
        self.addCleanup(checker.stop)

    def test_native_model_receives_actual_jpeg_bytes_and_selected_persona(self):
        set_setting("ai_persona_extra", "读图时逐项列出画面内容")
        sdk = fake_sdk()
        data = image_bytes("JPEG")
        with patch.object(LLMClient, "_build_openai", return_value=sdk), \
                patch.object(vision_service, "_read_config") as fallback:
            self.assertEqual(vision_service.describe_image(data, conversation=self.conv), "一张红色图片")
        fallback.assert_not_called()
        request = sdk.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "gpt-4o")
        self.assertIn("读图时逐项列出画面内容", request["messages"][0]["content"])
        image = request["messages"][1]["content"][0]["image_url"]["url"]
        self.assertTrue(image.startswith("data:image/jpeg;base64,"))
        self.assertEqual(base64.b64decode(image.split(",", 1)[1]), data)

    def test_native_config_works_without_separate_vision_credentials(self):
        with patch.object(vision_service, "_read_config") as fallback:
            self.assertTrue(vision_service.is_configured(self.conv))
        fallback.assert_not_called()

    def test_typed_text_response_ignores_non_answer_blocks(self):
        sdk = fake_sdk([{"type": "text", "text": "红色背景"},
                        {"type": "thinking", "thinking": "private reasoning"}])
        with patch.object(LLMClient, "_build_openai", return_value=sdk):
            self.assertEqual(vision_service.describe_image(image_bytes(), conversation=self.conv), "红色背景")

    def test_text_only_model_uses_selected_fallback(self):
        set_setting("llm_model", "private-text-model")
        fallback_cfg = config("private-vision-model")
        sdk = fake_sdk("备用图片描述")
        with patch.object(vision_service, "_read_config", return_value=fallback_cfg) as fallback, \
                patch.object(LLMClient, "_build_openai", return_value=sdk):
            self.assertEqual(vision_service.describe_image(image_bytes(), conversation=self.conv), "备用图片描述")
        fallback.assert_called_once_with(self.conv)
        self.assertEqual(sdk.chat.completions.create.call_args.kwargs["model"], "private-vision-model")

    def test_explicit_disable_routes_known_native_model_to_fallback(self):
        set_setting("llm_vision_capability", "off")
        self.assertIsNone(vision_service._native_config(self.conv))

    def test_conversation_vision_group_overrides_account_default(self):
        set_setting("llm_model", "text-only")
        set_setting("model_profiles:vision", {"active_id": "default-vlm", "items": [
            {"id": "default-vlm", "name": "默认视觉", "config": config("default-model")},
            {"id": "selected-vlm", "name": "本会话视觉", "config": config("selected-model")},
        ]})
        set_setting(f"chat_llm:{self.conv.id}", {"vision_profile_id": "selected-vlm"})
        sdk = fake_sdk("选中的视觉组")
        with patch.object(LLMClient, "_build_openai", return_value=sdk):
            self.assertEqual(vision_service.describe_image(image_bytes(), conversation=self.conv), "选中的视觉组")
        self.assertEqual(sdk.chat.completions.create.call_args.kwargs["model"], "selected-model")

    def test_explicit_enable_allows_private_vision_alias(self):
        set_setting("llm_model", "my-vision-alias")
        set_setting("llm_vision_capability", "on")
        self.assertEqual(vision_service._native_config(self.conv)["model"], "my-vision-alias")

    def test_native_failure_falls_back_with_visible_notice(self):
        sdk = fake_sdk()
        sdk.chat.completions.create.side_effect = [LLMError("native unavailable"),
                                                  {"choices": [{"message": {"content": "备用描述"}}]}]
        with patch.object(vision_service, "_read_config", return_value=config("backup-vlm")), \
                patch.object(LLMClient, "_build_openai", return_value=sdk):
            answer = vision_service.describe_image(image_bytes(), conversation=self.conv)
        self.assertIn("当前对话模型识图失败", answer)
        self.assertIn("备用描述", answer)
        self.assertEqual([call.kwargs["model"] for call in sdk.chat.completions.create.call_args_list],
                         ["gpt-4o", "backup-vlm"])

    def test_native_context_overflow_falls_back_without_sending_oversized_request(self):
        set_setting("llm_context_window_tokens", 32000)
        set_setting("llm_vision_capability", "on")
        fallback = {**config("large-context-vlm"), "context_window_tokens": 128000}
        sdk = fake_sdk("大窗口视觉结果")
        with patch.object(vision_service, "_read_config", return_value=fallback), \
                patch.object(LLMClient, "_build_openai", return_value=sdk):
            reply = vision_service.describe_image(image_bytes(), prompt="识别文字" * 6000,
                                                    conversation=self.conv)
        self.assertIn("已使用备用视觉配置", reply)
        self.assertIn("大窗口视觉结果", reply)
        self.assertEqual(sdk.chat.completions.create.call_count, 1)
        self.assertEqual(sdk.chat.completions.create.call_args.kwargs["model"], "large-context-vlm")

    def test_native_empty_result_does_not_claim_success(self):
        with patch.object(LLMClient, "_build_openai", return_value=fake_sdk("")), \
                patch.object(vision_service, "_read_config", return_value={}):
            with self.assertRaisesRegex(vision_service.VisionError, "识图失败"):
                vision_service.describe_image(image_bytes(), conversation=self.conv)

    def test_anthropic_native_image_request_uses_base64_source(self):
        set_setting("llm_protocol", "anthropic")
        set_setting("llm_base_url", "https://api.anthropic.com")
        set_setting("llm_model", "claude-sonnet-4-5")
        response = Mock(status_code=200)
        response.json.return_value = {"content": [{"type": "text", "text": "Claude 识图结果"}]}
        with patch("requests.post", return_value=response) as request:
            self.assertEqual(vision_service.describe_image(image_bytes(), conversation=self.conv), "Claude 识图结果")
        body = request.call_args.kwargs["json"]
        self.assertEqual(body["messages"][0]["content"][0]["type"], "image")
        self.assertEqual(body["messages"][0]["content"][0]["source"]["media_type"], "image/png")

    def test_foreign_conversation_is_rejected_before_any_model_call(self):
        other = self.make_user("vision-stranger")
        foreign = Conversation(user_id=other.id, title="private")
        db.session.add(foreign)
        db.session.commit()
        with patch.object(LLMClient, "_build_openai") as request:
            with self.assertRaises(ValueError):
                vision_service.describe_image(image_bytes(), conversation=foreign)
        request.assert_not_called()


class IncomingImageTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.user.feishu_open_id = "ou_vision_owner"
        self.conv = Conversation(user_id=self.user.id, title="image chat")
        db.session.add(self.conv)
        db.session.commit()
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.client_for(self.user)

    def post_image(self, data=None, **fields):
        response = self.client.post("/chat/api/image", data={
            "file": (BytesIO(image_bytes() if data is None else data), "image.png"), **fields,
        }, content_type="multipart/form-data")
        # Werkzeug spools multipart request bodies to disk for the size-limit case.
        response.request.input_stream.close()
        return response

    def test_web_image_upload_stores_private_asset_and_same_conversation(self):
        scopes = []

        def describe(data, **kwargs):
            scopes.append((current_user_id(), kwargs["conversation"].id, kwargs["prompt"]))
            return "有红色背景"

        with patch.object(vision_service, "is_configured", return_value=True), \
                patch.object(vision_service, "describe_image", side_effect=describe):
            response = self.post_image(conversation_id=str(self.conv.id), prompt="背景是什么颜色？")
        self.assertEqual(response.status_code, 200)
        result = response.get_json()["data"]
        self.assertTrue(result["recognized"])
        self.assertEqual(scopes, [(self.user.id, self.conv.id, "背景是什么颜色？")])
        asset = db.session.get(ImageAsset, result["image_id"])
        self.assertEqual(asset.user_id, self.user.id)
        self.assertFalse(asset.is_public)
        self.assertEqual((Path(self.app.config["IMAGE_DIR"]) / asset.file_path).read_bytes(), image_bytes())
        messages = Message.query.filter_by(conversation_id=self.conv.id).order_by(Message.id).all()
        self.assertEqual([msg.role for msg in messages], ["user", "assistant"])
        self.assertIn("背景是什么颜色", messages[0].content)
        self.assertIn("有红色背景", messages[1].content)

    def test_missing_vision_configuration_still_saves_image_and_explains(self):
        with patch.object(vision_service, "is_configured", return_value=False):
            response = self.post_image()
        result = response.get_json()["data"]
        self.assertFalse(result["recognized"])
        self.assertIn("暂无法识图", result["reply"])
        self.assertEqual(ImageAsset.query.count(), 1)
        self.assertEqual(Message.query.filter_by(conversation_id=result["conversation_id"]).count(), 2)

    def test_model_error_keeps_original_image_and_failure_history(self):
        with patch.object(vision_service, "is_configured", return_value=True), \
                patch.object(vision_service, "describe_image", side_effect=vision_service.VisionError("接口暂不可用")):
            response = self.post_image(conversation_id=str(self.conv.id))
        result = response.get_json()["data"]
        self.assertFalse(result["recognized"])
        self.assertIn("接口暂不可用", result["reply"])
        self.assertEqual(ImageAsset.query.count(), 1)
        self.assertEqual(Message.query.filter_by(conversation_id=self.conv.id).count(), 2)

    def test_foreign_and_unknown_conversations_return_404_without_saving(self):
        other = self.make_user("web-vision-stranger")
        foreign = Conversation(user_id=other.id, title="private")
        db.session.add(foreign)
        db.session.commit()
        for conv_id in (str(foreign.id), "99999", "not-an-id"):
            with self.subTest(conv_id=conv_id):
                self.assertEqual(self.post_image(conversation_id=conv_id).status_code, 404)
        self.assertEqual(ImageAsset.query.count(), 0)

    def test_fake_and_damaged_images_are_rejected_before_creating_a_chat(self):
        for data in (b"<script>alert(1)</script>", b"\x89PNG\r\n\x1a\ninvalid", image_bytes("JPEG")[:80]):
            with self.subTest(data=data[:16]):
                response = self.post_image(data)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["ok"])
        self.assertEqual(ImageAsset.query.count(), 0)
        self.assertEqual(Conversation.query.count(), 1)

    def test_large_image_is_rejected_with_413(self):
        from app.services.image_input_service import MAX_IMAGE_BYTES

        self.assertEqual(self.post_image(b"x" * (MAX_IMAGE_BYTES + 1)).status_code, 413)
        self.assertEqual(ImageAsset.query.count(), 0)

    def test_upload_requires_existing_csrf_protection(self):
        self.app.config["WTF_CSRF_ENABLED"] = True
        self.assertEqual(self.post_image().status_code, 400)
        self.assertEqual(ImageAsset.query.count(), 0)

    def test_feishu_image_uses_sender_scope_and_existing_chat_mapping(self):
        from app.services.feishu_inbound import _process_image

        other = self.make_user("feishu-vision-stranger")
        other.feishu_open_id = "ou_vision_stranger"
        db.session.commit()
        set_setting("feishu_chat_map", {"oc_same_chat": self.conv.id}, user_id=self.user.id)
        # A corrupt/malicious mapping must never cause writes to the other user.
        set_setting("feishu_chat_map", {"oc_same_chat": self.conv.id}, user_id=other.id)
        scopes = []

        def describe(data, **kwargs):
            scopes.append((current_user_id(), kwargs["conversation"].user_id))
            return f"图片属于用户 {current_user_id()}"

        with patch("app.services.channels.feishu_app.download_image_resource", return_value=image_bytes()), \
                patch("app.services.channels.feishu_app.send_text") as send, \
                patch.object(vision_service, "is_configured", return_value=True), \
                patch.object(vision_service, "describe_image", side_effect=describe):
            _process_image(self.app, "oc_same_chat", "message-one", "image-one", self.user.feishu_open_id)
            _process_image(self.app, "oc_same_chat", "message-two", "image-two", other.feishu_open_id)
        self.assertEqual(scopes, [(self.user.id, self.user.id), (other.id, other.id)])
        self.assertEqual(send.call_count, 4)
        self.assertEqual(Message.query.filter_by(conversation_id=self.conv.id).count(), 2)
        mapping = get_own_setting("feishu_chat_map", {}, user_id=other.id)
        self.assertNotEqual(mapping["oc_same_chat"], self.conv.id)
        self.assertEqual(ImageAsset.query.filter_by(user_id=self.user.id).count(), 1)
        self.assertEqual(ImageAsset.query.filter_by(user_id=other.id).count(), 1)
        self.assertEqual(current_user_id(), 0)

    def test_unbound_feishu_sender_does_not_download_or_save(self):
        from app.services.feishu_inbound import _process_image

        with patch("app.services.channels.feishu_app.download_image_resource") as download, \
                patch("app.services.channels.feishu_app.send_text") as send:
            _process_image(self.app, "oc_unknown", "message", "image", "ou_unbound")
        download.assert_not_called()
        self.assertIn("尚未绑定", send.call_args.args[1])
        self.assertEqual(ImageAsset.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
