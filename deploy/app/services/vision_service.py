"""视觉（多模态识图）服务：用 OpenAI 兼容的视觉模型识别图片内容。

与沟通模型（LLM）、图片生成模型（IMAGE）相互独立，配置优先级：settings 表 > .env：
    vision_base_url / vision_api_key / vision_model
    （.env 对应 VISION_BASE_URL / VISION_API_KEY / VISION_MODEL）

未配置时 is_configured() 返回 False，调用方收到图片仅保存、不识别。
"""
from __future__ import annotations

import base64

import requests

from app.services.settings_service import get_setting_from


class VisionError(Exception):
    """视觉模型调用失败（信息会回传调用方）。"""


def _read_config() -> dict:
    base = str(get_setting_from("vision_base_url", "VISION_BASE_URL", "") or "").strip().rstrip("/")
    key = str(get_setting_from("vision_api_key", "VISION_API_KEY", "") or "").strip()
    model = str(get_setting_from("vision_model", "VISION_MODEL", "") or "").strip()
    return {"base_url": base, "api_key": key, "model": model}


def is_configured() -> bool:
    cfg = _read_config()
    return bool(cfg["base_url"] and cfg["api_key"] and cfg["model"])


def describe_image(image_bytes: bytes, prompt: str = "请用简洁的中文描述这张图片",
                   image_format: str = "png") -> str:
    """把图片交给视觉模型，返回对图片的文字描述。

    :param image_bytes: 图片原始字节
    :param prompt: 识别指令
    :param image_format: 图片格式（png/jpg/webp/gif），用于构造 data URI
    """
    cfg = _read_config()
    if not is_configured():
        raise VisionError("视觉模型未配置：请先在「设置 → 视觉模型」填入 BaseURL / API Key / 模型")

    b64 = base64.b64encode(image_bytes).decode()
    data_uri = f"data:image/{image_format};base64,{b64}"
    payload = {
        "model": cfg["model"],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }],
        "max_tokens": 600,
        "temperature": 0.3,
    }
    try:
        resp = requests.post(
            f"{cfg['base_url']}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            timeout=90,
        )
    except requests.RequestException as e:
        raise VisionError(f"视觉模型请求失败：{str(e)[:150]}") from e

    if resp.status_code != 200:
        raise VisionError(f"视觉模型调用失败 HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        data = resp.json()
    except ValueError:
        raise VisionError(resp.text[:200]) from None

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise VisionError("视觉模型返回格式异常") from None
    if isinstance(content, list):  # 某些实现 content 是分片列表
        content = "".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content).strip()
