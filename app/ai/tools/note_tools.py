"""笔记 AI 工具：列表 / 新建 / 更新 / 检索（长期记忆素材）。

工具内时间展示用 user_tz(current_user) + fmt_dt 转为用户时区。
参数非法抛 ValueError，信息会回传给模型自我修正。
"""
from __future__ import annotations

from flask_login import current_user

from app.ai.registry import register_tool
from app.extensions import db
from app.models.note import Note
from app.services import note_service
from app.utils.timeutil import fmt_dt, user_tz


def _fmt_dt(dt) -> str:
    return fmt_dt(dt, user_tz(current_user))


@register_tool(
    name="list_notes",
    description=(
        "列出用户的笔记。q 为可选关键词，会模糊匹配笔记标题、内容和标签；"
        "不传 q 时返回最近更新的笔记列表。"
        "返回每条笔记的 id、标题、更新时间与标签，供概览或定位笔记使用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "q": {
                "type": "string",
                "description": "可选，搜索关键词（模糊匹配标题/内容/标签）；为空或不传则列出全部笔记。",
            },
        },
        "required": [],
    },
)
def list_notes(q=None):
    notes = note_service.list_notes(current_user.id, q=q or None, limit=50)
    if not notes:
        return "暂无笔记。"
    lines = [f"共 {len(notes)} 条笔记："]
    for n in notes:
        tags = "、".join(n.tags or []) or "无"
        lines.append(f"- [id={n.id}] {n.title}（更新于 {_fmt_dt(n.updated_at)}，标签：{tags}）")
    return "\n".join(lines)


@register_tool(
    name="create_note",
    description=(
        "创建一条新笔记（作为长期记忆保存）。title 为必填的笔记标题；"
        "content 为可选正文；tags 为可选的标签列表（如 ['工作','灵感']）或逗号分隔字符串。"
        "成功返回新笔记的 id 与标题。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "笔记标题，必填，不能为空。"},
            "content": {"type": "string", "description": "可选，笔记正文内容。"},
            "tags": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": "可选，标签列表或逗号分隔的字符串，如 ['工作','灵感'] 或 '工作,灵感'。",
            },
        },
        "required": ["title"],
    },
)
def create_note(title, content="", tags=None):
    note = note_service.create_note(current_user.id, title=title, content=content or "", tags=tags)
    return f"已创建笔记 [id={note.id}]：{note.title}"


@register_tool(
    name="update_note",
    description=(
        "更新已有的笔记。note_id 为必填的笔记 id（从 list_notes/search_notes 获取）；"
        "title、content、tags 均为可选项，传入的字段才会被修改，不传保持原样；"
        "tags 为标签列表或逗号分隔字符串。成功返回更新后的笔记 id 与标题。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "note_id": {"type": "integer", "description": "要更新的笔记 id，必填。"},
            "title": {"type": "string", "description": "可选，新的笔记标题。"},
            "content": {"type": "string", "description": "可选，新的笔记正文。"},
            "tags": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": "可选，新的标签列表或逗号分隔字符串。",
            },
        },
        "required": ["note_id"],
    },
)
def update_note(note_id, title=None, content=None, tags=None):
    try:
        note_id = int(note_id)
    except (TypeError, ValueError):
        raise ValueError(f"笔记 id 非法：{note_id}")
    note = db.session.get(Note, note_id)
    if note is None or note.deleted_at is not None or note.user_id != current_user.id:
        raise ValueError(f"笔记 {note_id} 不存在")
    fields = {}
    if title is not None:
        fields["title"] = title
    if content is not None:
        fields["content"] = content
    if tags is not None:
        fields["tags"] = tags
    note = note_service.update_note(note, **fields)
    return f"已更新笔记 [id={note.id}]：{note.title}"


@register_tool(
    name="search_notes",
    description=(
        "在用户的笔记中检索记忆。q 为必填的搜索关键词（模糊匹配标题/内容/标签），"
        "返回最相关笔记的 id、标题、标签与正文预览（前 120 字），供回答问题时引用笔记内容。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "q": {"type": "string", "description": "搜索关键词，必填，模糊匹配标题/内容/标签。"},
        },
        "required": ["q"],
    },
)
def search_notes(q):
    results = note_service.search_notes(current_user.id, q=q, limit=10)
    if not results:
        return "未找到相关笔记。"
    lines = [f"找到 {len(results)} 条相关笔记："]
    for r in results:
        tags = "、".join(r["tags"]) or "无"
        preview = r["content_preview"] or "（无正文）"
        lines.append(f"[id={r['id']}] {r['title']}（标签：{tags}）\n{preview}")
    return "\n\n".join(lines)
