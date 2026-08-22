"""AI 工具：今日概览与应用状态（元信息类，无副作用）。"""
from __future__ import annotations

from datetime import datetime

from flask_login import current_user

from app.ai.registry import register_tool
from app.models.scheduled_job import ScheduledJob
from app.utils.timeutil import day_bounds, expand_rrule, fmt_dt, user_tz, weekday_cn

try:
    from app.services import calendar_service

    _HAS_CALENDAR = True
except ImportError:  # pragma: no cover
    calendar_service = None
    _HAS_CALENDAR = False

try:
    from app.services import task_service

    _HAS_TASK = True
except ImportError:  # pragma: no cover
    task_service = None
    _HAS_TASK = False


@register_tool(
    name="get_today_summary",
    description=(
        "获取今日概览：今天的事件（含时间）、待办任务前 10 条、今日已完成任务数。"
        "适合回答“今天有什么安排”“今天要做什么”“今天完成了什么”类问题，无需参数。"
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)
def get_today_summary():
    """今日事件 + 待办任务 + 今日完成数，拼成中文摘要字符串。"""
    tz = user_tz(current_user)
    start, end = day_bounds(datetime.now(tz).date(), tz)
    parts: list[str] = []

    # 今日事件（含 rrule 展开）
    events: list[tuple] = []
    if _HAS_CALENDAR:
        for ev in calendar_service.list_events(start, end):
            if ev.rrule:
                for occ in expand_rrule(ev.rrule, ev.start_utc, start, end, tz):
                    events.append((ev, occ))
            else:
                events.append((ev, ev.start_utc))
    events.sort(key=lambda pair: pair[1])
    if events:
        lines = []
        for ev, occ in events:
            t = "全天" if ev.all_day else fmt_dt(occ, tz, "%H:%M")
            suffix = "（重复）" if ev.rrule else ""
            lines.append(f"{t} {ev.title}{suffix}")
        parts.append("今日事件：\n" + "\n".join(lines))
    else:
        parts.append("今日事件：无")

    # 待办任务前 10（含今日到期标注）
    if _HAS_TASK:
        open_tasks = task_service.list_tasks(status="open")
        if open_tasks:
            lines = []
            for t in open_tasks[:10]:
                if t.due_utc is None:
                    lines.append(f"- {t.title}（无截止时间）")
                elif start <= t.due_utc < end:
                    lines.append(f"- {t.title}（今日到期）")
                else:
                    lines.append(f"- {t.title}（截止 {fmt_dt(t.due_utc, tz)}）")
            parts.append("待办任务前 10：\n" + "\n".join(lines))
        else:
            parts.append("待办任务：无")

    # 今日已完成数
    done_count = 0
    if _HAS_TASK:
        from app.models.task import STATUS_DONE, Task

        done_count = Task.query.filter(
            Task.status == STATUS_DONE,
            Task.deleted_at.is_(None),
            Task.completed_at >= start,
            Task.completed_at < end,
        ).count()
    parts.append(f"今日已完成任务：{done_count} 个")

    return "\n\n".join(parts)


@register_tool(
    name="get_app_status",
    description=(
        "获取 灵犀 应用状态：AI 是否已配置、各通知渠道及其配置状态、"
        "定时任务总数与启用数。适合回答“系统状态”“AI 可用吗”“有哪些通知渠道”"
        "“定时任务情况”类问题，无需参数。"
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)
def get_app_status():
    """应用状态摘要：AI 配置、通知渠道、定时任务统计。"""
    from app.ai.llm import LLMClient
    from app.services import notify_service

    channels: list = []
    try:
        channels = notify_service.available_channels()
    except Exception:  # noqa: BLE001 —— 渠道模块未就绪时降级
        channels = []

    jobs = ScheduledJob.query.all()
    return {
        "llm_configured": LLMClient().is_configured,
        "channels": channels,
        "scheduled_jobs_total": len(jobs),
        "scheduled_jobs_enabled": sum(1 for j in jobs if j.enabled),
    }
