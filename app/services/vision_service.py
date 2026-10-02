"""图片识别：优先当前对话模型的原生视觉，回退到选中的独立视觉配置。"""
from __future__ import annotations

import base64
import logging

from app.ai.llm import LLMClient, LLMError, normalize_protocol
from app.services.model_capabilities import ContextWindowError

logger = logging.getLogger(__name__)


class VisionError(Exception):
    """视觉模型调用失败（信息会回传调用方）。"""


def _read_config(conversation=None) -> dict:
    from app.services.profile_service import resolve_profile_config

    return resolve_profile_config("vision", conversation=conversation)


def _complete(cfg: dict) -> bool:
    return bool(cfg.get("base_url") and cfg.get("api_key") and cfg.get("model"))


def _native_config(conversation=None) -> dict | None:
    from app.services.vision_capabilities import supports_native_vision

    cfg = LLMClient(conversation=conversation)._read_config()
    return cfg if _complete(cfg) and supports_native_vision(cfg) else None


def is_configured(conversation=None) -> bool:
    return bool(_native_config(conversation) or _complete(_read_config(conversation)))


class _VisionClient(LLMClient):
    """Use one private configuration snapshot for both OpenAI and Anthropic calls."""

    def __init__(self, cfg):
        super().__init__()
        self._vision_config = dict(cfg)
        self._vision_config["protocol"] = normalize_protocol(cfg.get("protocol", "openai"))
        self._vision_config.setdefault("timeout", 90)
        self._vision_config.setdefault("reasoning_effort", "default")

    def _read_config(self):
        return dict(self._vision_config)


def _image_content(image_bytes: bytes, prompt: str, image_format: str) -> list[dict]:
    if not image_bytes:
        raise VisionError("图片数据为空")
    if image_bytes.startswith(b"\xff\xd8"):
        image_format = "jpeg"
    elif image_bytes.startswith(b"\x89PNG"):
        image_format = "png"
    elif image_bytes.startswith(b"GIF8"):
        image_format = "gif"
    elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        image_format = "webp"
    image_format = str(image_format).lower().removeprefix("image/")
    if image_format == "jpg":
        image_format = "jpeg"
    if image_format not in {"png", "jpeg", "gif", "webp"}:
        raise VisionError("不支持的图片格式，请使用 PNG、JPEG、GIF 或 WebP")
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return [
        {"type": "image_url", "image_url": {"url": f"data:image/{image_format};base64,{encoded}"}},
        {"type": "text", "text": prompt},
    ]


def _describe(cfg, content, system_prompt="") -> str:
    messages = [{"role": "system", "content": system_prompt}] if system_prompt else []
    messages.append({"role": "user", "content": content})
    text, _ = _VisionClient(cfg).chat(messages)
    if not text or not text.strip():
        raise VisionError("视觉模型未返回识别文字")
    return text.strip()


def _system_prompt(conversation=None) -> str:
    from app.ai.prompts import build_system_prompt
    from app.extensions import db
    from app.models.user import User
    from app.utils.scoping import current_user_id

    uid = current_user_id()
    if conversation is not None and conversation.user_id != uid:
        raise VisionError("无权识别此会话的图片")
    user = db.session.get(User, uid) if uid else None
    return build_system_prompt(user, conversation=conversation)


def describe_image(image_bytes: bytes, prompt: str = "请用简洁的中文描述这张图片",
                   image_format: str = "png", conversation=None) -> str:
    """实际图片字节发给当前视觉模型；原生失败后显式提示备用识别结果。"""
    content = _image_content(image_bytes, prompt, image_format)
    native_cfg = _native_config(conversation)
    native_error = None
    if native_cfg:
        system_prompt = _system_prompt(conversation)
        try:
            return _describe(native_cfg, content, system_prompt)
        except (LLMError, VisionError, ContextWindowError) as e:
            native_error = e
            logger.warning("当前对话模型识图失败，将检查备用视觉配置：%s", type(e).__name__)

    cfg = _read_config(conversation)
    if not _complete(cfg):
        if native_error:
            raise VisionError("当前对话模型识图失败，且尚未配置备用视觉模型；请重试或切换模型") from native_error
        raise VisionError("当前对话模型未启用视觉能力，请在「设置 → 视觉」选择并配置一组视觉模型")
    try:
        text = _describe(cfg, content)
    except (LLMError, VisionError, ContextWindowError) as e:
        prefix = "当前对话模型和备用视觉模型识图均失败" if native_error else "视觉模型识图失败"
        raise VisionError(f"{prefix}：{str(e)[:180]}") from e
    if native_error:
        return "（当前对话模型识图失败，已使用备用视觉配置识别。）\n" + text
    return text
