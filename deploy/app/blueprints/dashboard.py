"""仪表盘：今日概览。直接查询模型，不依赖其他服务模块。"""
from __future__ import annotations

from flask import Blueprint, render_template
from flask_login import current_user, login_required

from app.extensions import db
from app.models.event import Event
from app.models.task import STATUS_OPEN, Task
from app.utils.timeutil import day_bounds, expand_rrule, user_tz, utcnow

bp = Blueprint("dashboard", __name__)


@bp.route("/")
@login_required
def index():
    tz = user_tz(current_user)
    now = utcnow()
    start, end = day_bounds(now.replace(tzinfo=None), tz)  # 今天 00:00 → 明天 00:00

    events = Event.query.filter(
        Event.deleted_at.is_(None),
        ((Event.end_utc.is_(None)) & (Event.start_utc >= start) & (Event.start_utc < end))
        | ((Event.end_utc.isnot(None)) & (Event.start_utc < end) & (Event.end_utc > start)),
    ).order_by(Event.start_utc).all()

    # 展开今日事件（含重复事件发生）
    today_events = []
    for ev in events:
        if ev.rrule:
            for occ in expand_rrule(ev.rrule, ev.start_utc, start, end, tz):
                today_events.append((ev, occ))
        else:
            today_events.append((ev, ev.start_utc))
    today_events.sort(key=lambda x: x[1])

    tasks_open = Task.query.filter_by(status=STATUS_OPEN, deleted_at=None).order_by(
        Task.due_utc.is_(None), Task.due_utc, Task.priority.desc()
    ).limit(10).all()
    tasks_due_today = [t for t in tasks_open if t.due_utc is not None and start <= t.due_utc < end]

    done_today = Task.query.filter(
        Task.status == "done", Task.deleted_at.is_(None),
        Task.completed_at >= start, Task.completed_at < end,
    ).count()

    return render_template(
        "dashboard/index.html",
        today_events=today_events,
        tasks_open=tasks_open,
        tasks_due_today=tasks_due_today,
        done_today=done_today,
        tz_name=str(tz),
    )
