"""记忆 AI 工具：手动记录 / 查看 / 删除长期记忆。"""
from __future__ import annotations

from flask_login import current_user

from app.ai.registry import register_tool
from app.services import memory_service
from app.utils.timeutil import fmt_dt, user_tz


def _find(memory_id):
    try:
        memory_id = int(memory_id)
    except (TypeError, ValueError):
        raise ValueError("memory_id 必须是数字") from None
    memory = memory_service.get_memory(memory_id, current_user.id)
    if memory is None:
        raise ValueError(f"记忆 {memory_id} 不存在或已删除")
    return memory


@register_tool(
    name="remember",
    description=(
        "记录一条长期记忆（用户明确要求记住的事实、偏好、习惯等）。"
        "content 为要记住的内容，必填；importance 为重要度 1-5（默认 3，5 最重要）；"
        "expires 为过期时间（用户本地时区字符串，如 2026-09-01 或 2026-09-01 18:00，"
        "时间敏感的信息建议填写，长期有效省略）。"
        "记录后会自动注入后续每次对话的上下文。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要记住的内容，必填"},
            "importance": {"type": "integer", "enum": [1, 2, 3, 4, 5],
                           "description": "重要度 1-5，默认 3"},
            "expires": {"type": "string",
                        "description": "过期时间（用户本地时区字符串），时间敏感信息填写"},
        },
        "required": ["content"],
    },
)
def remember(content: str, importance: int = 3, expires: str | None = None):
    from app.utils.timeutil import parse_local, user_tz

    expires_utc = None
    if expires and str(expires).strip():
        expires_utc = parse_local(str(expires).strip(), user_tz(current_user))
        if expires_utc is None:
            raise ValueError(
                f"无法解析过期时间: {expires}（请用如 2026-09-01 或 2026-09-01 18:00）")
    try:
        memory = memory_service.remember(current_user.id, content, importance=importance,
                                         expires_at=expires_utc)
    except ValueError as e:
        raise ValueError(str(e)) from e
    return f"已记住 [id={memory.id}]：{memory.content[:100]}（重要度 {memory.importance}）"


@register_tool(
    name="list_memories",
    description=(
        "列出长期记忆（AI 已知的关于用户的事实与偏好）。"
        "返回 id/content/importance（重要度 1-5）/expires（过期时间）/source/更新时间。"
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)
def list_memories():
    tz = user_tz(current_user)
    rows = memory_service.list_memories(current_user.id, limit=50)
    if not rows:
        return "还没有长期记忆。"
    return [
        {"id": m.id, "content": m.content,
         "importance": m.importance,
         "expires": fmt_dt(m.expires_at, tz) or "",
         "source": "手动" if m.source == "manual" else "自动",
         "updated_at": fmt_dt(m.updated_at, tz)}
        for m in rows
    ]


@register_tool(
    name="delete_memory",
    description="删除一条长期记忆（软删除，不可恢复，确认用户明确要求后再调用）。memory_id 必填。",
    parameters={
        "type": "object",
        "properties": {"memory_id": {"type": "integer", "description": "记忆 ID，必填"}},
        "required": ["memory_id"],
    },
    dangerous=True,
)
def delete_memory(memory_id: int):
    memory = _find(memory_id)
    memory_service.soft_delete_memory(memory)
    return f"🗑️ 已删除记忆 [id={memory.id}]"
