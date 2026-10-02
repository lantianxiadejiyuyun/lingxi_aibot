"""图片 AI 工具：生成 / 列表 / 详情 / AI 改图 / 公开开关 / 删除。

- generate_image 调用 OpenAI 兼容 images 接口，图片落盘后返回 Markdown 图片链接
  （![](...)），助手回复中引用即可在对话里直接显示
- edit_image 走图像编辑接口，端点不支持时自动降级为按描述重新生成（parent_id 关联）
- 参数非法/接口失败抛 ValueError，信息回传模型自我修正
"""
from __future__ import annotations

import logging
from pathlib import Path

from flask import current_app
from flask_login import current_user

from app.ai.registry import register_tool
from app.services import image_service
from app.utils.scoping import current_user_id
from app.utils.timeutil import fmt_dt, user_tz

logger = logging.getLogger(__name__)


def _find(image_id):
    try:
        image_id = int(image_id)
    except (TypeError, ValueError):
        raise ValueError("image_id 必须是数字") from None
    uid = current_user_id()
    if not uid:
        raise ValueError("请先登录后访问图片")
    asset = image_service.get_image(image_id, uid)
    if asset is None:
        raise ValueError(f"图片 {image_id} 不存在或已删除")
    return asset


def _json(asset) -> dict:
    """图片 → 模型可读 JSON（含访问地址与公开性）。"""
    tz = user_tz(current_user)
    return {
        "id": asset.id,
        "prompt": asset.prompt,
        "instruction": asset.instruction,
        "model": asset.model,
        "size": asset.size,
        "url": image_service.asset_url(asset),
        "is_public": asset.is_public,
        "parent_id": asset.parent_id,
        "created_at": fmt_dt(asset.created_at, tz),
    }


@register_tool(
    name="generate_image",
    description=(
        "生成一张图片（AI 绘画）。prompt 为画面描述（必填，中文或英文皆可，"
        "建议写清主体/风格/构图/光线）；size 为尺寸（可选，如 1024x1024、"
        "720x1280、1280x720，默认用设置里的默认尺寸）。"
        "返回图片的 id、访问地址与 Markdown 图片链接；"
        "回复用户时请引用返回的 Markdown 图片链接（![](...)）以便对话中直接显示图片。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "画面描述提示词，必填"},
            "size": {"type": "string", "description": "图片尺寸 宽x高，可选，如 1024x1024 / 720x1280 / 1280x720"},
        },
        "required": ["prompt"],
    },
)
def generate_image(prompt: str, size: str | None = None):
    try:
        asset = image_service.generate(current_user.id, prompt, size=size or None)
    except image_service.ImageError as e:
        raise ValueError(str(e)) from e
    url = image_service.asset_url(asset)
    return {
        "id": asset.id,
        "url": url,
        "markdown": f"![{asset.prompt[:30]}]({url})",
        "size": asset.size,
    }


@register_tool(
    name="list_images",
    description=(
        "列出最近生成或上传的图片。返回每张图片的 id、提示词摘要、尺寸、公开性、"
        "创建时间与访问地址；这些仅为元数据，不代表看过图片。"
        "需要知道实际画面内容时必须调用 read_image。"
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)
def list_images():
    assets = image_service.list_images(current_user.id, limit=50)
    if not assets:
        return "还没有生成过图片。"
    tz = user_tz(current_user)
    return [
        {
            "id": a.id,
            "prompt": (a.prompt or "")[:80],
            "size": a.size,
            "is_public": a.is_public,
            "created_at": fmt_dt(a.created_at, tz),
            "url": image_service.asset_url(a),
        }
        for a in assets
    ]


@register_tool(
    name="get_image",
    description=(
        "获取某张图片的详细信息（完整提示词、修改要求、公开性、访问地址）。"
        "image_id 为图片 ID（从 list_images 获取）。修改图片前可先用它查看原提示词。"
        "结果仅为元数据，不能据此描述画面；要查看实际内容必须调用 read_image。"
    ),
    parameters={
        "type": "object",
        "properties": {"image_id": {"type": "integer", "description": "图片 ID，必填"}},
        "required": ["image_id"],
    },
)
def get_image(image_id: int):
    asset = _find(image_id)
    return _json(asset)


@register_tool(
    name="read_image",
    description=(
        "读取当前用户图片库中的图片内容，image_id 来自上传记录或 list_images。"
        "将真实图片交给当前会话模型识别，无法识图时自动使用已配置的备用视觉模型；"
        "prompt 可指定需要观察的问题。不会公开图片或更改任何配置，"
        "不受 AI 自主切换开关限制。必须实际调用成功后才能根据识别结果回答，"
        "不得把文件名、提示词或 get_image 元数据当成画面内容。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "image_id": {"type": "integer", "description": "当前用户图片库中的图片 ID，必填"},
            "prompt": {"type": "string", "description": "希望识别的画面内容或问题，可选，最多 4000 字"},
        },
        "required": ["image_id"],
    },
)
def read_image(image_id: int, prompt: str | None = None):
    from app.services import vision_service
    from app.services.image_input_service import DEFAULT_PROMPT, MAX_IMAGE_BYTES, validate_image
    from app.services.model_control_service import current_conversation

    conversation, user = current_conversation()
    if current_user_id() != user.id:
        raise ValueError("无权读取此会话的图片")
    asset = _find(image_id)
    if prompt is not None and not isinstance(prompt, str):
        raise ValueError("识图问题必须是文字")
    question = (prompt or "").strip() or DEFAULT_PROMPT
    if len(question) > 4000:
        raise ValueError("识图问题最多 4000 字")
    try:
        root = Path(current_app.config["IMAGE_DIR"]).resolve()
        relative = Path(asset.file_path)
        if relative.is_absolute():
            raise ValueError("图片文件不可读取，请重新上传")
        source = (root / relative).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError("图片文件不可读取，请重新上传")
        if source.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError("图片不能超过 8 MiB")
        with source.open("rb") as stream:
            data = stream.read(MAX_IMAGE_BYTES + 1)
    except (OSError, RuntimeError) as e:
        raise ValueError("图片文件不可读取，请重新上传") from e
    image_format = validate_image(data)
    try:
        description = vision_service.describe_image(
            data, prompt=question, image_format=image_format, conversation=conversation,
        )
    except Exception as e:  # noqa: BLE001 —— 不把上游凭据、文件路径或原始异常传给模型
        logger.warning("图片读取识别失败 image=%s: %s", asset.id, type(e).__name__)
        raise ValueError("图片识别失败，未获得画面内容；请重试或检查当前模型与备用视觉配置") from e
    if not isinstance(description, str) or not description.strip():
        raise ValueError("图片识别失败，模型未返回画面内容")
    return {"id": asset.id, "description": description.strip()}


@register_tool(
    name="edit_image",
    description=(
        "按用户要求修改已有图片（AI 改图）。image_id 为要修改的图片 ID（必填）；"
        "instruction 为修改要求（必填，如“把背景改成黄昏”“换成动漫风格”“去掉文字水印”）。"
        "平台支持图像编辑时走编辑接口，否则自动按修改要求重新生成一张新图。"
        "无论哪种方式都会生成一张新图（不改动原图），返回新图的 id 与 Markdown 图片链接，"
        "回复用户时请引用该链接以便对话中直接显示。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "image_id": {"type": "integer", "description": "要修改的图片 ID，必填"},
            "instruction": {"type": "string", "description": "修改要求描述，必填"},
        },
        "required": ["image_id", "instruction"],
    },
)
def edit_image(image_id: int, instruction: str):
    asset = _find(image_id)
    try:
        new_asset = image_service.edit(asset, instruction)
    except image_service.ImageError as e:
        raise ValueError(str(e)) from e
    url = image_service.asset_url(new_asset)
    return {
        "id": new_asset.id,
        "url": url,
        "markdown": f"![修改后-{new_asset.id}]({url})",
        "parent_id": new_asset.parent_id,
        "size": new_asset.size,
    }


@register_tool(
    name="set_image_public",
    description=(
        "设置图片的公开性。image_id 为图片 ID（必填）；is_public=true 表示任何人可通过链接访问"
        "（适合嵌入到生成的网页里），false 表示仅登录后可见。返回设置结果。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "image_id": {"type": "integer", "description": "图片 ID，必填"},
            "is_public": {"type": "boolean", "description": "是否公开，必填"},
        },
        "required": ["image_id", "is_public"],
    },
)
def set_image_public(image_id: int, is_public: bool):
    asset = _find(image_id)
    image_service.set_public(asset, is_public)
    return (f"图片 {asset.id} 已设为{'公开' if asset.is_public else '仅登录可见'}"
            f"（地址：{image_service.asset_url(asset)}）")


@register_tool(
    name="delete_image",
    description="删除图片（软删除，不可恢复，确认用户明确要求后再调用）。image_id 必填。",
    parameters={
        "type": "object",
        "properties": {"image_id": {"type": "integer", "description": "图片 ID，必填"}},
        "required": ["image_id"],
    },
    dangerous=True,
)
def delete_image(image_id: int):
    asset = _find(image_id)
    image_service.soft_delete(asset)
    return f"🗑️ 已删除图片 {asset.id}（提示词：{(asset.prompt or '')[:40]}）"
