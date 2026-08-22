"""任务服务：任务 CRUD、软删除、到期扫描（调度动作）。

时间约定：数据库一律 naive UTC（utcnow()）；对外展示/接收用用户时区。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from app.extensions import db
from app.models.task import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_OPEN,
    TASK_PRIORITIES,
    Task,
)
from app.scheduler import register_action
from app.services.notify_service import notify
from app.utils.timeutil import fmt_dt, utcnow

# update_task 允许更新的字段
_TASK_FIELDS = ("title", "notes", "due_utc", "priority", "project", "tags")


def _clean_tags(tags) -> list:
    """tags（list[str] 或 None）→ 去空白、去空、去重保序的字符串列表。"""
    if not tags:
        return []
    result = []
    for tag in tags:
        tag = str(tag).strip()
        if tag and tag not in result:
            result.append(tag)
    return result


def list_tasks(user_id: Optional[int] = None, status=None, q=None, due_before=None,
               due_after=None, include_deleted=False) -> list[Task]:
    """查询任务列表（可按 user 隔离）。

    - 默认过滤软删除（include_deleted=True 时包含）
    - status: "open"/"done"/"cancelled" 或 None（不过滤）
    - q: 模糊匹配 title/notes/project（不区分大小写）
    - due_before/due_after: naive UTC 时间边界
    - 排序：due 空值在后 → due 升序 → priority 降序
    """
    query = Task.query
    if user_id is not None:
        query = query.filter(Task.user_id == user_id)
    if not include_deleted:
        query = query.filter(Task.deleted_at.is_(None))
    if status:
        query = query.filter(Task.status == status)
    if q:
        like = f"%{q}%"
        query = query.filter(
            Task.title.ilike(like) | Task.notes.ilike(like) | Task.project.ilike(like)
        )
    if due_before is not None:
        query = query.filter(Task.due_utc < due_before)
    if due_after is not None:
        query = query.filter(Task.due_utc > due_after)
    return query.order_by(
        Task.due_utc.is_(None), Task.due_utc, Task.priority.desc()
    ).all()


def create_task(user_id: int, title, due_naive=None, priority=2, notes="", project="",
                tags=None) -> Task:
    """创建任务。due_naive 为 naive UTC；tags 为 list[str]，默认 []。"""
    title = (title or "").strip()
    if not title:
        raise ValueError("任务标题不能为空")
    if priority not in TASK_PRIORITIES:
        raise ValueError("优先级必须是 1-3")
    task = Task(
        user_id=user_id,
        title=title,
        due_utc=due_naive,
        priority=int(priority),
        notes=notes or "",
        project=project or "",
        tags=_clean_tags(tags),
        status=STATUS_OPEN,
    )
    db.session.add(task)
    db.session.commit()
    return task


def update_task(task, **fields) -> Task:
    """更新任务字段（title/notes/due_utc/priority/project/tags），未知字段忽略。"""
    for key, value in fields.items():
        if key not in _TASK_FIELDS:
            continue
        if key == "title":
            value = (value or "").strip()
            if not value:
                raise ValueError("任务标题不能为空")
        elif key == "priority":
            if value not in TASK_PRIORITIES:
                raise ValueError("优先级必须是 1-3")
            value = int(value)
        elif key == "tags":
            value = _clean_tags(value)
        setattr(task, key, value)
    db.session.commit()
    return task


def set_task_status(task, status):
    """设置状态：open/done/cancelled，否则 ValueError；置 done 写完成时间，其他清空。"""
    if status not in (STATUS_OPEN, STATUS_DONE, STATUS_CANCELLED):
        raise ValueError("非法状态，必须是 open/done/cancelled")
    task.status = status
    if status == STATUS_DONE:
        task.completed_at = utcnow()
    else:
        task.completed_at = None
    db.session.commit()


def soft_delete_task(task):
    """软删除：仅置 deleted_at。"""
    task.deleted_at = utcnow()
    db.session.commit()


def due_tasks(user_id: Optional[int] = None, now_naive=None, horizon_minutes=30) -> list[Task]:
    """到期扫描：open、未删除、due 落在 (now, now+horizon]。"""
    now = now_naive or utcnow()
    end = now + timedelta(minutes=horizon_minutes)
    q = Task.query.filter(
        Task.deleted_at.is_(None),
        Task.status == STATUS_OPEN,
        Task.due_utc.isnot(None),
        Task.due_utc > now,
        Task.due_utc <= end,
    )
    if user_id is not None:
        q = q.filter(Task.user_id == user_id)
    return q.order_by(Task.due_utc).all()


@register_action("task_due_scan", description="扫描即将到期的任务并发送通知")
def scan_task_due(user, params: dict | None = None):
    """调度动作：该用户的任务到期提醒（按场景渠道）。params 支持 horizon_minutes。"""
    from flask import current_app

    from app.services.notify_service import notify_for

    params = params or {}
    try:
        horizon = max(1, int(params.get("horizon_minutes", 30)))
    except (TypeError, ValueError):
        horizon = 30
    tz = ZoneInfo(current_app.config.get("APP_TIMEZONE", "Asia/Shanghai"))
    tasks = due_tasks(user.id, horizon_minutes=horizon)
    for t in tasks:
        notify_for("reminder", "📌 任务即将到期", f"「{t.title}」截止于 {fmt_dt(t.due_utc, tz)}",
                   user_id=user.id)
