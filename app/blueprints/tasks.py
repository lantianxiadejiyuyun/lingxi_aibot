"""任务页面与 JSON API（PRG + fetch）。"""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from app.extensions import csrf
from app.models.task import STATUS_DONE, STATUS_OPEN, TASK_PRIORITIES, Task
from app.services import task_service
from app.utils.timeutil import humanize_relative, parse_local, to_user, user_tz, utcnow

bp = Blueprint("tasks", __name__, url_prefix="/tasks")

_STATUSES = ("open", "done", "cancelled", "all")
_PRIORITY_LABELS = {1: "低", 2: "中", 3: "高"}


def _task_row(t: Task, tz, now: datetime) -> dict:
    """Task → 模板渲染用字典（展示时间已转用户时区）。"""
    due_local = to_user(t.due_utc, tz)
    return {
        "id": t.id,
        "title": t.title,
        "notes": t.notes,
        "priority": t.priority,
        "status": t.status,
        "project": t.project,
        "tags": t.tags or [],
        "due_text": humanize_relative(t.due_utc, tz),
        "due_local": due_local.strftime("%Y-%m-%dT%H:%M") if due_local else "",
        "overdue": bool(t.due_utc and t.due_utc < now and t.status == STATUS_OPEN),
        "done": t.status == STATUS_DONE,
        "cancelled": t.status == "cancelled",
    }


def _get_task(task_id):
    """按 id 取当前用户的未删除任务，非法/不存在/非本用户返回 None。"""
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return None
    return Task.query.filter(Task.id == tid, Task.deleted_at.is_(None),
                             Task.user_id == current_user.id).first()


def _parse_tags(raw) -> list:
    """逗号分隔字符串 → 标签列表（去空白、去空）。"""
    if raw is None:
        return []
    return [s.strip() for s in str(raw).split(",") if s.strip()]


def _json_ok(data=None):
    return jsonify({"ok": True, "data": data})


def _json_err(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


@bp.route("/")
@login_required
def index():
    status = request.args.get("status", STATUS_OPEN)
    if status not in _STATUSES:
        status = STATUS_OPEN
    q = (request.args.get("q") or "").strip()
    priority_raw = request.args.get("priority") or ""
    priority = None
    if priority_raw.isdigit() and int(priority_raw) in TASK_PRIORITIES:
        priority = int(priority_raw)

    tasks = task_service.list_tasks(current_user.id, status=None if status == "all" else status,
                                    q=q or None)
    if priority is not None:
        tasks = [t for t in tasks if t.priority == priority]

    tz = user_tz(current_user)
    rows = [_task_row(t, tz, utcnow()) for t in tasks]
    return render_template(
        "tasks/index.html",
        tasks=rows,
        priority_labels=_PRIORITY_LABELS,
        status=status,
        q=q,
        priority=str(priority) if priority is not None else "",
        tz_name=str(tz),
    )


@bp.route("/api/create", methods=["POST"])
@csrf.exempt
@login_required
def api_create():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return _json_err("任务标题不能为空")

    priority_raw = data.get("priority", 2)
    try:
        priority = int(priority_raw) if priority_raw not in (None, "") else 2
    except (TypeError, ValueError):
        priority = 2

    due = None
    due_raw = data.get("due")
    if due_raw:
        due = parse_local(due_raw, user_tz(current_user))
        if due is None:
            return _json_err("时间格式无法解析")

    try:
        task = task_service.create_task(
            current_user.id,
            title=title,
            due_naive=due,
            priority=priority,
            notes=data.get("notes") or "",
            project=data.get("project") or "",
            tags=_parse_tags(data.get("tags")),
        )
    except ValueError as e:
        return _json_err(str(e))
    return _json_ok({"id": task.id, "title": task.title, "status": task.status})


@bp.route("/api/update", methods=["POST"])
@csrf.exempt
@login_required
def api_update():
    data = request.get_json(silent=True) or {}
    task = _get_task(data.get("task_id"))
    if task is None:
        return _json_err("任务不存在或已删除")

    fields = {}
    if data.get("title") is not None:
        fields["title"] = data["title"]
    if data.get("notes") is not None:
        fields["notes"] = data["notes"]
    if data.get("project") is not None:
        fields["project"] = data["project"]
    if data.get("priority") is not None:
        try:
            fields["priority"] = int(data["priority"])
        except (TypeError, ValueError):
            return _json_err("优先级必须是 1-3")
    if "due" in data and data["due"] is not None:
        due_raw = data["due"]
        if due_raw:
            due = parse_local(due_raw, user_tz(current_user))
            if due is None:
                return _json_err("时间格式无法解析")
            fields["due_utc"] = due
        else:
            fields["due_utc"] = None  # 空字符串 → 清除截止时间
    if data.get("tags") is not None:
        fields["tags"] = _parse_tags(data["tags"])
    if data.get("status") is not None:
        if data["status"] not in ("open", "done", "cancelled"):
            return _json_err("非法状态")
        try:
            task_service.set_task_status(task, data["status"])
        except ValueError as e:
            return _json_err(str(e))

    if fields:
        try:
            task_service.update_task(task, **fields)
        except ValueError as e:
            return _json_err(str(e))
    return _json_ok({"id": task.id, "title": task.title,
                     "status": task.status, "priority": task.priority})


@bp.route("/api/toggle", methods=["POST"])
@csrf.exempt
@login_required
def api_toggle():
    data = request.get_json(silent=True) or {}
    task = _get_task(data.get("task_id"))
    if task is None:
        return _json_err("任务不存在或已删除")
    new_status = STATUS_OPEN if task.status == STATUS_DONE else STATUS_DONE
    task_service.set_task_status(task, new_status)
    return _json_ok({"id": task.id, "status": new_status,
                     "done": new_status == STATUS_DONE})


@bp.route("/api/delete", methods=["POST"])
@csrf.exempt
@login_required
def api_delete():
    data = request.get_json(silent=True) or {}
    task = _get_task(data.get("task_id"))
    if task is None:
        return _json_err("任务不存在或已删除")
    task_service.soft_delete_task(task)
    return _json_ok({"id": task.id})
