"""AI 工具：任务管理（查询 / 创建 / 更新 / 完成 / 删除）。

工具内时间参数为用户时区字符串，按约定用 parse_local(text, user_tz(current_user))
转 naive UTC 存储；返回给模型的时间用 fmt_dt 转好的用户时区字符串。
"""
from __future__ import annotations

from flask import current_app
from flask_login import current_user

from app.ai.registry import register_tool
from app.models.task import Task
from app.services import task_service
from app.utils.timeutil import fmt_dt, get_tz, parse_local


def _tz():
    """工具调用上下文时区：优先当前用户，缺省应用配置。"""
    name = getattr(current_user, "timezone", None) \
        or current_app.config.get("APP_TIMEZONE", "Asia/Shanghai")
    return get_tz(name)


def _find_task(task_id, user_id) -> Task:
    """按 id 取当前用户的未删除任务；非法、不存在或不属于该用户抛 ValueError（信息回给模型自纠）。"""
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        raise ValueError("task_id 必须是数字") from None
    task = Task.query.filter(Task.id == tid, Task.user_id == user_id,
                             Task.deleted_at.is_(None)).first()
    if task is None:
        raise ValueError(f"任务 {tid} 不存在或已删除")
    return task


def _summary(task: Task) -> dict:
    """任务摘要（时间已转用户时区字符串）。"""
    tz = _tz()
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "project": task.project,
        "tags": task.tags or [],
        "due": fmt_dt(task.due_utc, tz),
    }


@register_tool(
    "list_tasks",
    "查询任务列表。status 可选值：open（进行中，默认）、done（已完成）、cancelled（已取消）、all（全部）。"
    "q 为模糊搜索关键字，匹配标题/备注/项目。返回最多 30 条任务的摘要（id/title/due/priority/status/project/tags），"
    "due 为用户本地时区时间字符串，无截止时间为空字符串。",
    {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["open", "done", "cancelled", "all"],
                "description": "任务状态筛选，默认 open（进行中）",
            },
            "q": {
                "type": "string",
                "description": "模糊搜索关键字，匹配标题/备注/项目，可空",
            },
        },
        "required": [],
    },
)
def list_tasks(status: str = "open", q: str | None = None):
    if status not in ("open", "done", "cancelled", "all"):
        raise ValueError("status 必须是 open/done/cancelled/all 之一")
    tasks = task_service.list_tasks(current_user.id, status=None if status == "all" else status,
                                    q=q or None)
    return [_summary(t) for t in tasks[:30]]


@register_tool(
    "create_task",
    "创建新任务。title 必填；due 为截止时间，用户本地时区字符串"
    "（如 '2026-03-20 18:00' 或 '2026-03-20'，可空）；priority 优先级 1-3"
    "（1 低、2 中、3 高，默认 2）；notes 备注；project 所属项目；tags 标签列表。"
    "返回创建后的任务摘要。",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "任务标题，必填"},
            "due": {
                "type": "string",
                "description": "截止时间，用户本地时区字符串（如 '2026-03-20 18:00'），可空",
            },
            "priority": {
                "type": "integer", "enum": [1, 2, 3],
                "description": "优先级 1-3，默认 2（中）",
            },
            "notes": {"type": "string", "description": "任务备注，可空"},
            "project": {"type": "string", "description": "所属项目名，可空"},
            "tags": {
                "type": "array", "items": {"type": "string"},
                "description": "标签列表，可空",
            },
        },
        "required": ["title"],
    },
)
def create_task(title: str, due: str | None = None, priority: int = 2,
                notes: str = "", project: str = "", tags: list | None = None):
    due_naive = None
    if due:
        due_naive = parse_local(due, _tz())
        if due_naive is None:
            raise ValueError(
                f"无法解析截止时间: {due}（请用如 '2026-03-20 18:00' 的用户本地时区格式）")
    try:
        task = task_service.create_task(
            current_user.id,
            title=title,
            due_naive=due_naive,
            priority=priority,
            notes=notes or "",
            project=project or "",
            tags=tags,
        )
    except ValueError as e:
        raise ValueError(str(e)) from e
    return _summary(task)


@register_tool(
    "update_task",
    "更新已有任务。task_id 必填；其余参数只改传入的字段（传 null 表示不改）。"
    "due 为用户本地时区时间字符串；status 可设为 open/done/cancelled。"
    "返回更新后的任务摘要。",
    {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "任务 ID，必填"},
            "title": {"type": "string", "description": "新标题，不改则传 null"},
            "due": {
                "type": "string",
                "description": "新截止时间（用户本地时区字符串），不改则传 null",
            },
            "priority": {
                "type": "integer", "enum": [1, 2, 3],
                "description": "新优先级，不改则传 null",
            },
            "notes": {"type": "string", "description": "新备注，不改则传 null"},
            "project": {"type": "string", "description": "新项目名，不改则传 null"},
            "status": {
                "type": "string", "enum": ["open", "done", "cancelled"],
                "description": "新状态，不改则传 null",
            },
        },
        "required": ["task_id"],
    },
)
def update_task(task_id: int, title: str | None = None, due: str | None = None,
                priority: int | None = None, notes: str | None = None,
                project: str | None = None, status: str | None = None):
    task = _find_task(task_id, current_user.id)
    fields = {}
    if title is not None:
        fields["title"] = title
    if due is not None:
        due_naive = parse_local(due, _tz())
        if due_naive is None:
            raise ValueError(
                f"无法解析截止时间: {due}（请用如 '2026-03-20 18:00' 的用户本地时区格式）")
        fields["due_utc"] = due_naive
    if priority is not None:
        fields["priority"] = priority
    if notes is not None:
        fields["notes"] = notes
    if project is not None:
        fields["project"] = project
    if status is not None:
        task_service.set_task_status(task, status)
    if fields:
        task_service.update_task(task, **fields)
    return _summary(task)


@register_tool(
    "complete_task",
    "将任务标记为已完成（status 置为 done，记录完成时间）。task_id 必填。",
    {
        "type": "object",
        "properties": {"task_id": {"type": "integer", "description": "任务 ID，必填"}},
        "required": ["task_id"],
    },
)
def complete_task(task_id: int):
    task = _find_task(task_id, current_user.id)
    task_service.set_task_status(task, "done")
    return f"✅ 任务「{task.title}」已完成"


@register_tool(
    "delete_task",
    "删除任务（软删除，仅从列表隐藏）。task_id 必填。此操作不可恢复，"
    "请在确认用户明确要求删除后再调用。",
    {
        "type": "object",
        "properties": {"task_id": {"type": "integer", "description": "任务 ID，必填"}},
        "required": ["task_id"],
    },
    dangerous=True,
)
def delete_task(task_id: int):
    task = _find_task(task_id, current_user.id)
    title = task.title
    task_service.soft_delete_task(task)
    return f"🗑️ 已删除任务「{title}」"
