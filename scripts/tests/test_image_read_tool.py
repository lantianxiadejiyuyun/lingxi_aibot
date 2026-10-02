"""Stored images are really read within the bound conversation, without publishing."""
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from scripts.tests.support import IsolatedAppTestCase

from app.ai.tools.image_tools import read_image
from app.extensions import db
from app.models.conversation import Conversation
from app.models.image import ImageAsset
from app.services import image_service, vision_service
from app.services.image_input_service import MAX_IMAGE_BYTES
from app.services.model_control_service import bind_conversation
from app.services.settings_service import get_own_setting, set_setting
from app.utils.scoping import set_current_user_id, user_scope
from app.utils.timeutil import utcnow


class ImageReadToolTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.other = self.make_user("other_image_owner")
        set_current_user_id(self.user.id)
        self.conversation = Conversation(user_id=self.user.id, title="read stored image")
        db.session.add(self.conversation)
        db.session.commit()
        buffer = BytesIO()
        Image.new("RGB", (3, 2), "blue").save(buffer, format="JPEG")
        self.data = buffer.getvalue()
        self.asset = self.make_asset()

    def make_asset(self, owner=None, path=None, public=False):
        asset = ImageAsset(user_id=(owner or self.user).id, prompt="元数据不代表实际画面",
                           file_path=path if path is not None else image_service.save_bytes(self.data),
                           is_public=public)
        db.session.add(asset)
        db.session.commit()
        return asset

    def call(self, image_id=None, prompt=None):
        with bind_conversation(self.conversation, self.user):
            return read_image(self.asset.id if image_id is None else image_id, prompt)

    def test_private_original_is_sent_with_current_conversation_without_mutation(self):
        settings = {"profile_id": "chosen-chat", "vision_profile_id": "chosen-backup", "auto_switch": False}
        set_setting(f"chat_llm:{self.conversation.id}", settings)
        with patch.object(vision_service, "describe_image", return_value=" 蓝色画面 ") as describe:
            result = self.call(prompt="看看是什么颜色")
        self.assertEqual(result, {"id": self.asset.id, "description": "蓝色画面"})
        describe.assert_called_once_with(self.data, prompt="看看是什么颜色", image_format="jpeg",
                                         conversation=self.conversation)
        db.session.expire(self.asset)
        self.assertFalse(self.asset.is_public)
        self.assertEqual(get_own_setting(f"chat_llm:{self.conversation.id}"), settings)
        self.assertEqual(ImageAsset.query.count(), 1)
        self.assertNotIn("url", result)
        self.assertNotIn("file_path", result)

    def test_defaults_to_description_prompt(self):
        with patch.object(vision_service, "describe_image", return_value="实际识别") as describe:
            self.call()
        self.assertIn("描述这张图片", describe.call_args.kwargs["prompt"])

    def test_another_users_image_is_not_read_even_when_public(self):
        foreign = self.make_asset(owner=self.other, public=True)
        with patch.object(vision_service, "describe_image") as describe:
            with self.assertRaisesRegex(ValueError, "不存在或已删除"):
                self.call(foreign.id)
        describe.assert_not_called()

    def test_deleted_image_is_not_read(self):
        self.asset.deleted_at = utcnow()
        db.session.commit()
        with patch.object(vision_service, "describe_image") as describe:
            with self.assertRaisesRegex(ValueError, "不存在或已删除"):
                self.call()
        describe.assert_not_called()

    def test_missing_bound_conversation_rejects_request(self):
        with patch.object(vision_service, "describe_image") as describe:
            with self.assertRaisesRegex(ValueError, "当前聊天会话"):
                read_image(self.asset.id)
        describe.assert_not_called()

    def test_mismatched_user_scope_rejects_bound_conversation(self):
        with user_scope(self.other.id), patch.object(vision_service, "describe_image") as describe:
            with self.assertRaisesRegex(ValueError, "无权"):
                self.call()
        describe.assert_not_called()

    def test_missing_and_outside_paths_are_rejected_before_model_call(self):
        outside = Path(self.temp_directory.name) / "private-outside.jpg"
        outside.write_bytes(self.data)
        paths = ("../private-outside.jpg", str(outside), "not-present.jpg", ".")
        for path in paths:
            with self.subTest(path=path), patch.object(vision_service, "describe_image") as describe:
                self.asset.file_path = path
                db.session.commit()
                with self.assertRaisesRegex(ValueError, "不可读取"):
                    self.call()
                describe.assert_not_called()

    def test_resolved_symlink_outside_image_root_is_rejected(self):
        outside = Path(self.temp_directory.name) / "private-outside.jpg"
        outside.write_bytes(self.data)
        link = Path(self.app.config["IMAGE_DIR"]) / "linked-outside.jpg"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("Creating a symlink requires additional privileges on this host")
        self.asset.file_path = link.name
        db.session.commit()
        with patch.object(vision_service, "describe_image") as describe:
            with self.assertRaisesRegex(ValueError, "不可读取"):
                self.call()
        describe.assert_not_called()

    def test_oversized_or_invalid_file_is_rejected(self):
        source = Path(self.app.config["IMAGE_DIR"]) / self.asset.file_path
        for contents, message in ((b"x" * (MAX_IMAGE_BYTES + 1), "8 MiB"), (b"not-image", "图片无效")):
            with self.subTest(message=message), patch.object(vision_service, "describe_image") as describe:
                source.write_bytes(contents)
                with self.assertRaisesRegex(ValueError, message):
                    self.call()
                describe.assert_not_called()

    def test_failed_provider_response_never_leaks_credentials_or_local_path(self):
        for exception in (vision_service.VisionError("secret api_key=sk-secret /private/image"),
                          RuntimeError("unexpected access_token=private")):
            with self.subTest(error=type(exception).__name__), \
                    patch.object(vision_service, "describe_image", side_effect=exception):
                with self.assertRaises(ValueError) as caught:
                    self.call()
                self.assertIn("未获得画面内容", str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))
                self.assertNotIn("private", str(caught.exception))

    def test_empty_provider_response_is_failure(self):
        with patch.object(vision_service, "describe_image", return_value="  "):
            with self.assertRaisesRegex(ValueError, "未返回画面内容"):
                self.call()

    def test_invalid_question_does_not_call_model(self):
        for question in ("x" * 4001, {"not": "text"}):
            with self.subTest(question_type=type(question).__name__), \
                    patch.object(vision_service, "describe_image") as describe:
                with self.assertRaises(ValueError):
                    self.call(prompt=question)
                describe.assert_not_called()
