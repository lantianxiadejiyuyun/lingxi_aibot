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
from app.utils.timeutil import expand_rrule, fmt_dt, user_tz, utcnow

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
                user_id: Optional[int] = None, include_deleted: bool = False) -> list[Event]:
    """返回区间内的候选事件；重复事件须由调用方展开后过滤具体发生时间。"""
    query = Event.query
    if user_id is not None:
        query = query.filter(Event.user_id == user_id)
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
    # 重复事件的首个 start/end 可能早于当前窗口，不能在展开规则前排除。
    recurring = (Event.rrule != "") & (Event.start_utc < end_naive)
    return query.filter(overlap | recurring).order_by(Event.start_utc).all()


def get_event(event_id: int, user_id: Optional[int] = None) -> Optional[Event]:
    """按 id 取未删除事件，不存在、已软删除或非本用户返回 None。"""
    if event_id is None:
        return None
    ev = db.session.get(Event, event_id)
    if ev is None or ev.deleted_at is not None:
        return None
    if user_id is not None and ev.user_id != user_id:
        return None
    return ev


def create_event(user_id: int, title: str, start_naive: datetime,
                 end_naive: Optional[datetime] = None, all_day: bool = False,
                 description: str = "", rrule: str = "",
                 reminder_minutes: Optional[int] = None, location: str = "") -> Event:
    """创建事件并落库（commit），返回对象。"""
    ev = Event(
        user_id=user_id,
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
    if any(key in fields and fields[key] != getattr(event, key)
           for key in ("start_utc", "rrule", "reminder_minutes")):
        event.last_reminded_occurrence_utc = None
    for key in _UPDATABLE_FIELDS:
        if key in fields:
            setattr(event, key, fields[key])
    db.session.commit()
    return event


def soft_delete_event(event: Event) -> None:
    """软删除：置 deleted_at 并 commit。"""
    event.deleted_at = utcnow()
    db.session.commit()


def _reminders_between(user_id, start, end, tz) -> list[tuple[Event, datetime]]:
    """按提醒时间的 [start, end) 区间查询，而非按事件开始时间查询。"""
    q = Event.query.filter(
        Event.deleted_at.is_(None),
        Event.reminder_minutes.isnot(None),
    )
    if user_id is not None:
        q = q.filter(Event.user_id == user_id)
    events = q.all()

    results: list[tuple[Event, datetime]] = []
    for ev in events:
        offset = timedelta(minutes=ev.reminder_minutes)
        if ev.rrule:
            occurrences = expand_rrule(ev.rrule, ev.start_utc, start + offset, end + offset, tz)
        else:
            occurrences = [ev.start_utc]
        for occ in occurrences:
            remind = occ - offset
            if start <= remind < end:
                results.append((ev, occ))
    results.sort(key=lambda pair: pair[1] - timedelta(minutes=pair[0].reminder_minutes))
    return results


def upcoming_reminders(user_id: Optional[int] = None, now_naive: Optional[datetime] = None,
                       horizon_minutes: int = 60) -> list[tuple[Event, datetime]]:
    """预览未来 (now, now+horizon] 的提醒；调度发送使用 due_reminders。"""
    now = now_naive or utcnow()
    tick = timedelta(microseconds=1)
    return _reminders_between(user_id, now + tick,
                              now + timedelta(minutes=horizon_minutes) + tick, _app_tz())


def due_reminders(user_id: int, now_naive: Optional[datetime] = None,
                  lookback_minutes: int = 60, tz=None) -> list[tuple[Event, datetime]]:
    """返回已到提醒时间且尚未发送的发生，允许补发最近一小时错过的扫描。"""
    now = now_naive or utcnow()
    rows = _reminders_between(user_id, now - timedelta(minutes=lookback_minutes),
                              now + timedelta(microseconds=1), tz or _app_tz())
    return [(ev, occ) for ev, occ in rows
            if ev.last_reminded_occurrence_utc is None or ev.last_reminded_occurrence_utc < occ]


@register_action(
    "event_reminder_scan",
    description="发送已到期且未发送的日程提醒（lookback_minutes：补发窗口，默认 60 分钟）",
)
def scan_event_reminders(user, params: Optional[dict] = None) -> None:
    """调度动作：对该用户的每个到期提醒按场景渠道发送通知。"""
    from app.services.notify_service import notify_for

    params = params or {}
    try:
        lookback = int(params.get("lookback_minutes", params.get("horizon_minutes")) or 60)
    except (TypeError, ValueError):
        lookback = 60
    if lookback < 1:
        lookback = 60

    tz = user_tz(user)
    for ev, occ in due_reminders(user.id, lookback_minutes=lookback, tz=tz):
        # 先持久化领取结果：并发扫描与重启均不会再次发送同一次发生。
        # 外部渠道的失败仍由通知中心记录，避免自动重试导致部分成功的渠道重复推送。
        claimed = Event.query.filter(
            Event.id == ev.id, Event.user_id == user.id, Event.deleted_at.is_(None),
            Event.last_reminded_occurrence_utc.is_(None) | (Event.last_reminded_occurrence_utc < occ),
        ).update({Event.last_reminded_occurrence_utc: occ}, synchronize_session=False)
        db.session.commit()
        if not claimed:
            continue
        parts = [f"时间：{fmt_dt(occ, tz)}"]
        if ev.location:
            parts.append(f"地点：{ev.location}")
        if ev.description:
            parts.append(f"详情：{ev.description}")
        notify_for("reminder", "⏰ 日程提醒：" + ev.title, "\n".join(parts), user_id=user.id)
