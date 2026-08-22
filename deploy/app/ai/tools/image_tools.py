"""图片 AI 工具：生成 / 列表 / 详情 / AI 改图 / 公开开关 / 删除。

- generate_image 调用 OpenAI 兼容 images 接口，图片落盘后返回 Markdown 图片链接
  （![](...)），助手回复中引用即可在对话里直接显示
- edit_image 走图像编辑接口，端点不支持时自动降级为按描述重新生成（parent_id 关联）
- 参数非法/接口失败抛 ValueError，信息回传模型自我修正
"""
from __future__ import annotations

from flask_login import current_user

from app.ai.registry import register_tool
from app.services import image_service
from app.utils.timeutil import fmt_dt, user_tz


def _find(image_id):
    try:
        image_id = int(image_id)
    except (TypeError, ValueError):
        raise ValueError("image_id 必须是数字") from None
    asset = image_service.get_image(image_id)
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
        asset = image_service.generate(prompt, size=size or None)
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
        "列出最近的生成图片。返回每张图片的 id、提示词摘要、尺寸、公开性、"
        "创建时间与访问地址（不含图片二进制）。适合回答“我生成过哪些图片”类问题。"
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)
def list_images():
    assets = image_service.list_images(limit=50)
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
