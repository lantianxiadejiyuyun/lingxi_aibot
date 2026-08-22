"""日历服务：事件 CRUD（软删除）、重复规则、提醒扫描与调度动作。

时间约定：数据库一律 naive UTC；重复展开与提醒计算使用应用时区
（app.config["APP_TIMEZONE"]，调度线程无请求上下文，不使用 current_user）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from flask import current_app

from app.extensions import db
from app.models.event import Event
from app.scheduler import register_action
from app.utils.timeutil import expand_rrule, fmt_dt, utcnow

# 前端/AI 的 repeat 取值 → RFC5545 规则字符串
REPEAT_MAP: dict[str, str] = {
    "none": "",
    "daily": "FREQ=DAILY;INTERVAL=1",
    "weekly": "FREQ=WEEKLY;INTERVAL=1",
    "monthly": "FREQ=MONTHLY;INTERVAL=1",
    "yearly": "FREQ=YEARLY;INTERVAL=1",
}


def rrule_to_repeat(rrule: str) -> str:
    """RFC5545 规则字符串 → repeat 键（none/daily/weekly/monthly/yearly）。"""
    freq = (rrule or "").upper()
    for key, rule in REPEAT_MAP.items():
        if rule and rule in freq:
            return key
    return "none"


def _app_tz() -> ZoneInfo:
    name = "Asia/Shanghai"
    try:
        name = current_app.config.get("APP_TIMEZONE") or name
    except Exception:  # noqa: BLE001 —— 无请求上下文时回退默认
        pass
    return ZoneInfo(name)


def list_events(start_naive: datetime, end_naive: datetime,
                include_deleted: bool = False) -> list[Event]:
    """返回与 [start_naive, end_naive) 时间重叠的事件，按开始时间排序。

    end_utc 为空（点事件）时按 start 是否落在区间内判断。
    """
    query = Event.query
    if not include_deleted:
        query = query.filter(Event.deleted_at.is_(None))
    overlap = (
        (Event.end_utc.is_(None))
        & (Event.start_utc >= start_naive)
        & (Event.start_utc < end_naive)
    ) | (
        (Event.end_utc.isnot(None))
        & (Event.start_utc < end_naive)
        & (Event.end_utc > start_naive)
    )
    return query.filter(overlap).order_by(Event.start_utc).all()


def get_event(event_id: int) -> Optional[Event]:
    """按 id 取未删除事件，不存在或已软删除返回 None。"""
    if event_id is None:
        return None
    ev = db.session.get(Event, event_id)
    if ev is None or ev.deleted_at is not None:
        return None
    return ev


def create_event(title: str, start_naive: datetime, end_naive: Optional[datetime] = None,
                 all_day: bool = False, description: str = "", rrule: str = "",
                 reminder_minutes: Optional[int] = None, location: str = "") -> Event:
    """创建事件并落库（commit），返回对象。"""
    ev = Event(
        title=title,
        description=description or "",
        start_utc=start_naive,
        end_utc=end_naive,
        all_day=bool(all_day),
        rrule=rrule or "",
        reminder_minutes=reminder_minutes,
        location=location or "",
    )
    db.session.add(ev)
    db.session.commit()
    return ev


# update_event 允许更新的字段（key 即 Event 属性名）
_UPDATABLE_FIELDS = (
    "title", "description", "start_utc", "end_utc",
    "all_day", "rrule", "reminder_minutes", "location",
)


def update_event(event: Event, **fields) -> Event:
    """更新事件字段（只接受白名单字段，忽略其他），commit 后返回对象。"""
    for key in _UPDATABLE_FIELDS:
        if key in fields:
            setattr(event, key, fields[key])
    db.session.commit()
    return event


def soft_delete_event(event: Event) -> None:
    """软删除：置 deleted_at 并 commit。"""
    event.deleted_at = utcnow()
    db.session.commit()


def upcoming_reminders(now_naive: Optional[datetime] = None,
                       horizon_minutes: int = 60) -> list[tuple[Event, datetime]]:
    """返回未来 (now, now+horizon] 内需要提醒的事件。

    返回 [(event, 发生时间), ...]：发生时间由重复规则展开得到（应用时区），
    提醒触发时间 = 发生时间 - reminder_minutes。
    """
    now = now_naive or utcnow()
    horizon_end = now + timedelta(minutes=horizon_minutes)
    scan_end = now + timedelta(hours=24)
    tz = _app_tz()

    events = Event.query.filter(
        Event.deleted_at.is_(None),
        Event.reminder_minutes.isnot(None),
    ).all()

    results: list[tuple[Event, datetime]] = []
    for ev in events:
        if ev.rrule:
            occurrences = expand_rrule(ev.rrule, ev.start_utc, now, scan_end, tz)
        else:
            occurrences = [ev.start_utc]
        for occ in occurrences:
            remind = occ - timedelta(minutes=ev.reminder_minutes)
            if now < remind <= horizon_end:
                results.append((ev, occ))
    results.sort(key=lambda pair: pair[1] - timedelta(minutes=pair[0].reminder_minutes))
    return results


@register_action(
    "event_reminder_scan",
    description="扫描即将到来的日程提醒并发送通知（参数：horizon_minutes 提前量分钟数）",
)
def scan_event_reminders(params: Optional[dict] = None) -> None:
    """调度动作：对每个到期提醒按场景渠道发送通知。"""
    from app.services.notify_service import notify_for

    params = params or {}
    try:
        horizon = int(params.get("horizon_minutes") or 60)
    except (TypeError, ValueError):
        horizon = 60
    if horizon < 1:
        horizon = 60

    tz = _app_tz()
    for ev, occ in upcoming_reminders(horizon_minutes=horizon):
        parts = [f"时间：{fmt_dt(occ, tz)}"]
        if ev.location:
            parts.append(f"地点：{ev.location}")
        if ev.description:
            parts.append(f"详情：{ev.description}")
        notify_for("reminder", "⏰ 日程提醒：" + ev.title, "\n".join(parts))
