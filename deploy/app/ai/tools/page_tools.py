"""网页 AI 工具：创建 / 查询 / 修改 / 复制 / 删除自由 HTML 页面（网页生成器）。

- content 为完整 HTML 源码（含 <!DOCTYPE html>），AI 直接编写
- slug 为访问地址标识（公开页面：网页域名/<slug>；私有页面：后台域名/p/<slug>），不传自动生成
- 参数非法抛 ValueError，信息回传给模型自我修正
"""
from __future__ import annotations

from flask_login import current_user

from app.ai.registry import register_tool
from app.services import page_service
from app.utils.timeutil import fmt_dt, user_tz


def _page_json(page, include_content: bool = True) -> dict:
    """页面 → 模型可读的 JSON（时间转用户时区字符串）。"""
    tz = user_tz(current_user)
    data = {
        "id": page.id,
        "title": page.title,
        "slug": page.slug,
        "description": page.description,
        "is_public": page.is_public,
        "enabled": page.enabled,
        "updated_at": fmt_dt(page.updated_at, tz),
    }
    if include_content:
        data["content"] = page.content
    return data


def _find_page(page_id):
    try:
        page_id = int(page_id)
    except (TypeError, ValueError):
        raise ValueError("page_id 必须是数字") from None
    page = page_service.get_page(page_id)
    if page is None:
        raise ValueError(f"网页 {page_id} 不存在或已删除")
    return page


@register_tool(
    name="list_pages",
    description=(
        "列出所有已创建的网页。返回每个页面的 id、标题、slug（访问地址标识）、"
        "简介、是否公开（is_public）、是否显示（enabled）与更新时间。"
        "适合回答“我有哪些网页”“网页 xxx 在哪个地址”类问题。"
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)
def list_pages():
    pages = page_service.list_pages()
    if not pages:
        return "还没有创建过网页。"
    return [_page_json(p, include_content=False) for p in pages[:50]]


@register_tool(
    name="get_page",
    description=(
        "获取某个网页的完整内容（含 HTML 源码）。page_id 为网页 ID（从 list_pages 获取）。"
        "修改网页前应先用它读取当前内容。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "page_id": {"type": "integer", "description": "网页 ID，必填"},
        },
        "required": ["page_id"],
    },
)
def get_page(page_id: int):
    page = _find_page(page_id)
    return _page_json(page, include_content=True)


@register_tool(
    name="create_page",
    description=(
        "创建一个新网页。title 为页面标题（必填）；content 为完整 HTML 源码（必填，"
        "含 <!DOCTYPE html>、<html>、<head>、<body> 等完整结构，CSS/JS 内联其中，"
        "不要用 Markdown 代码围栏包裹）；slug 为访问地址标识（可选，2-64 位小写字母/"
        "数字/连字符，不传自动生成）；description 为页面简介（可选）；"
        "is_public=true 表示任何人可通过链接访问，false 表示仅登录用户可见；"
        "enabled 表示是否显示（false 则前台不可访问）。返回新页面的 id/slug/公开地址。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "页面标题，必填"},
            "content": {"type": "string", "description": "完整 HTML 源码，必填"},
            "slug": {"type": "string", "description": "访问地址标识，可选，2-64 位小写字母/数字/连字符"},
            "description": {"type": "string", "description": "页面简介，可选"},
            "is_public": {"type": "boolean", "description": "是否公开访问，默认 true"},
            "enabled": {"type": "boolean", "description": "是否显示，默认 true"},
        },
        "required": ["title", "content"],
    },
)
def create_page(title: str, content: str, slug: str | None = None,
                description: str = "", is_public: bool = True, enabled: bool = True):
    try:
        page = page_service.create_page(
            title=title,
            content=content or "",
            slug=slug or None,
            description=description or "",
            is_public=is_public,
            enabled=enabled,
        )
    except ValueError as e:
        raise ValueError(str(e)) from e
    return (f"已创建网页 [id={page.id}]：「{page.title}」"
            f"（slug={page.slug}，{'公开' if page.is_public else '仅登录可见'}，"
            f"{'显示中' if page.enabled else '已隐藏'}）。"
            f"访问地址：{page_service.page_public_url(page)}")


@register_tool(
    name="update_page",
    description=(
        "修改已有网页。page_id 为网页 ID（必填）；其余参数只修改传入的字段（不传表示不改）。"
        "content 为完整 HTML 源码；slug 为新的访问地址标识；"
        "is_public 控制是否公开；enabled 控制是否显示（隐藏后前台 404）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "page_id": {"type": "integer", "description": "网页 ID，必填"},
            "title": {"type": "string", "description": "新标题，可选"},
            "content": {"type": "string", "description": "新完整 HTML 源码，可选"},
            "slug": {"type": "string", "description": "新访问地址标识，可选"},
            "description": {"type": "string", "description": "新简介，可选"},
            "is_public": {"type": "boolean", "description": "是否公开，可选"},
            "enabled": {"type": "boolean", "description": "是否显示，可选"},
        },
        "required": ["page_id"],
    },
)
def update_page(page_id: int, title: str | None = None, content: str | None = None,
                slug: str | None = None, description: str | None = None,
                is_public: bool | None = None, enabled: bool | None = None):
    page = _find_page(page_id)
    try:
        page_service.update_page(
            page, title=title, content=content, slug=slug, description=description,
            is_public=is_public, enabled=enabled,
        )
    except ValueError as e:
        raise ValueError(str(e)) from e
    return (f"已更新网页 [id={page.id}]：「{page.title}」（slug={page.slug}，"
            f"{'公开' if page.is_public else '仅登录可见'}，"
            f"{'显示中' if page.enabled else '已隐藏'}）")


@register_tool(
    name="duplicate_page",
    description="复制已有网页为新页面（标题加「副本」后缀，内容相同，自动生成新地址）。page_id 必填。",
    parameters={
        "type": "object",
        "properties": {"page_id": {"type": "integer", "description": "网页 ID，必填"}},
        "required": ["page_id"],
    },
)
def duplicate_page(page_id: int):
    page = _find_page(page_id)
    new_page = page_service.duplicate_page(page)
    return f"已复制网页 [id={page.id}] → 新网页 [id={new_page.id}]「{new_page.title}」（slug={new_page.slug}）"


@register_tool(
    name="delete_page",
    description="删除网页（软删除，不可恢复，确认用户明确要求后再调用）。page_id 必填。",
    parameters={
        "type": "object",
        "properties": {"page_id": {"type": "integer", "description": "网页 ID，必填"}},
        "required": ["page_id"],
    },
    dangerous=True,
)
def delete_page(page_id: int):
    page = _find_page(page_id)
    title = page.title
    page_service.soft_delete_page(page)
    return f"🗑️ 已删除网页「{title}」"
