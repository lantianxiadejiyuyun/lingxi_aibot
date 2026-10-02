"""网页和飞书共享的收图、原生视觉识别与会话记录流程。"""
from __future__ import annotations

from io import BytesIO
import logging
import warnings

from PIL import Image

from app.extensions import db
from app.models.conversation import Conversation, Message
from app.models.image import ImageAsset
from app.services import image_service, vision_service
from app.services.context_service import conversation_lock
from app.utils.scoping import user_scope
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 8 * 1024 * 1024
DEFAULT_PROMPT = "请用简洁的中文描述这张图片"


class ImageInputError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def validate_image(data: bytes) -> str:
    """Decode the image instead of trusting an upload name or Content-Type."""
    if not data:
        raise ImageInputError("请选择一张图片")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageInputError("图片不能超过 8 MiB", 413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as picture:
                image_format = str(picture.format or "").lower()
                if image_format not in {"png", "jpeg", "gif", "webp"}:
                    raise ImageInputError("仅支持 PNG、JPEG、GIF 或 WebP 图片")
                picture.verify()
            # JPEG verify() only checks headers; loading also rejects truncated data.
            with Image.open(BytesIO(data)) as picture:
                picture.load()
    except ImageInputError:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombWarning,
            Image.DecompressionBombError) as e:
        raise ImageInputError("图片无效、已损坏或尺寸过大，请换一张图片") from e
    return image_format


def receive_image(user, data: bytes, conversation=None, prompt="", *, on_identify=None) -> dict:
    """Save privately, identify within the owner's scope, and keep both chat turns."""
    if conversation is not None and conversation.user_id != user.id:
        raise ImageInputError("会话不存在", 404)
    image_format = validate_image(data)
    prompt = str(prompt or DEFAULT_PROMPT).strip() or DEFAULT_PROMPT
    if len(prompt) > 4000:
        raise ImageInputError("识图问题最多 4000 字")
    with user_scope(user.id):
        if conversation is None:
            conversation = Conversation(user_id=user.id, title="图片对话" if prompt == DEFAULT_PROMPT else prompt[:20])
            db.session.add(conversation)
            db.session.commit()
        with conversation_lock(conversation.id):
            fname = image_service.save_bytes(data)
            asset = ImageAsset(user_id=user.id, prompt=prompt, file_path=fname, is_public=False)
            db.session.add(asset)
            db.session.flush()
            db.session.add(Message(conversation_id=conversation.id, role="user",
                                   content=f"（发送图片，图片库 ID #{asset.id}）\n{prompt}"))
            db.session.commit()
            recognized = False
            try:
                if vision_service.is_configured(conversation=conversation):
                    if on_identify:
                        try:
                            on_identify()
                        except Exception:  # noqa: BLE001 —— 进度通知失败不阻断已保存图片的识别
                            logger.warning("图片识别进度通知失败")
                    desc = vision_service.describe_image(data, prompt=prompt, image_format=image_format,
                                                         conversation=conversation)
                    reply = f"🖼️ 已收到图片并保存（ID #{asset.id}）。识别结果：\n{desc}"
                    recognized = True
                else:
                    reply = (f"🖼️ 已收到图片并保存到图片库（ID #{asset.id}）。"
                             "当前会话模型已跳过读图或接口未配置完整，且未配置备用视觉模型，暂无法识图。")
            except Exception as e:  # noqa: BLE001 —— 识别失败仍保留用户图片和失败记录
                logger.warning("视觉识别失败 conversation=%s: %s", conversation.id, type(e).__name__)
                reply = f"🖼️ 已保存图片（ID #{asset.id}），但识别失败：{str(e)[:180]}"
            db.session.add(Message(conversation_id=conversation.id, role="assistant", content=reply))
            conversation.updated_at = utcnow()
            db.session.commit()
            return {"conversation_id": conversation.id, "reply": reply,
                    "image_id": asset.id, "recognized": recognized}
