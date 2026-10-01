"""仪表盘：今日概览。"""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, render_template
from flask_login import current_user, login_required

from app.models.task import STATUS_OPEN, Task
from app.services import calendar_service
from app.utils.timeutil import day_bounds, expand_rrule, to_user, user_tz

bp = Blueprint("dashboard", __name__)


@bp.route("/")
@login_required
def index():
    tz = user_tz(current_user)
    # “今天”按用户本地时区日期计算（UTC 日期在本地凌晨会差一天）
    start, end = day_bounds(datetime.now(tz).date(), tz)  # 今天 00:00 → 明天 00:00

    events = calendar_service.list_events(start, end, current_user.id)

    # 展开今日事件（含重复事件发生），发生时间转用户时区供模板直接展示
    today_events = []
    for ev in events:
        if ev.rrule:
            for occ in expand_rrule(ev.rrule, ev.start_utc, start, end, tz):
                today_events.append((ev, to_user(occ, tz)))
        else:
            today_events.append((ev, to_user(ev.start_utc, tz)))
    today_events.sort(key=lambda x: x[1])

    tasks_open = Task.query.filter_by(status=STATUS_OPEN, deleted_at=None,
                                      user_id=current_user.id).order_by(
        Task.due_utc.is_(None), Task.due_utc, Task.priority.desc()
    ).limit(10).all()
    tasks_due_today = [t for t in tasks_open if t.due_utc is not None and start <= t.due_utc < end]
    # 任务截止日期转用户时区（避免 UTC 日期跨午夜显示错一天）
    due_dates = {
        t.id: to_user(t.due_utc, tz).strftime("%m-%d")
        for t in tasks_open if t.due_utc is not None
    }

    done_today = Task.query.filter(
        Task.status == "done", Task.deleted_at.is_(None), Task.user_id == current_user.id,
        Task.completed_at >= start, Task.completed_at < end,
    ).count()

    return render_template(
        "dashboard/index.html",
        today_events=today_events,
        tasks_open=tasks_open,
        tasks_due_today=tasks_due_today,
        due_dates=due_dates,
        done_today=done_today,
        tz_name=str(tz),
    )
