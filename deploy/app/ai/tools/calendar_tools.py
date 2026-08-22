"""日历 AI 工具：查询 / 创建 / 更新 / 删除事件。

时间参数均为用户时区字符串（YYYY-MM-DD 或 YYYY-MM-DD HH:MM），
按约定用 user_tz(current_user) + parse_local 转 naive UTC。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from flask_login import current_user

from app.ai.registry import register_tool
from app.services import calendar_service
from app.utils.timeutil import (
    expand_rrule, fmt_dt, parse_local, to_naive_utc, to_user, user_tz,
)

REPEAT_MAP = calendar_service.REPEAT_MAP

_TIME_DESC = "用户本地时区时间字符串，格式 YYYY-MM-DD 或 YYYY-MM-DD HH:MM"


def _tz():
    return user_tz(current_user)


def _parse(text: str, tz, all_day: bool = False) -> datetime:
    """解析用户时区时间字符串 → naive UTC；失败抛 ValueError。"""
    if not text or not str(text).strip():
        raise ValueError(f"时间不能为空，请使用{_TIME_DESC}")
    dt = parse_local(str(text).strip().replace("T", " "), tz)
    if dt is None:
        raise ValueError(f"时间格式不正确：{text}，请使用{_TIME_DESC}")
    if all_day:
        local = to_user(dt, tz)
        dt = to_naive_utc(datetime.combine(local.date(), time.min, tzinfo=tz))
    return dt


def _repeat_rrule(repeat: str) -> str:
    rrule = REPEAT_MAP.get((repeat or "none").strip())
    if rrule is None:
        raise ValueError("repeat 取值非法，应为 none/daily/weekly/monthly/yearly")
    return rrule


def _reminder(raw) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        val = int(raw)
    except (TypeError, ValueError):
        raise ValueError("reminder_minutes 必须是整数") from None
    if val < 0:
        raise ValueError("reminder_minutes 不能为负数")
    return val


def _find_conflicts(start_naive, end_naive, exclude_id=None) -> list[dict]:
    """检测与既有事件（含重复展开）的时间重叠，返回冲突摘要列表。"""
    tz = _tz()
    if end_naive is None:
        end_naive = start_naive
    margin = timedelta(days=1)
    candidates = calendar_service.list_events(start_naive - margin, end_naive + margin)
    conflicts: list[dict] = []
    for ev in candidates:
        if ev.id == exclude_id:
            continue
        if ev.rrule:
            occs = expand_rrule(ev.rrule, ev.start_utc, start_naive - margin,
                                end_naive + margin, tz)
        else:
            occs = [ev.start_utc]
        for occ in occs:
            ev_end = occ + (ev.end_utc - ev.start_utc) if ev.end_utc else occ
            if occ < end_naive and ev_end > start_naive:
                conflicts.append({
                    "id": ev.id,
                    "title": ev.title + ("（重复）" if ev.rrule else ""),
                    "start": fmt_dt(occ, tz),
                    "end": fmt_dt(ev_end, tz) if ev.end_utc else "",
                })
                break
    return conflicts


def _conflict_msg(conflicts: list[dict]) -> str:
    """冲突提示文案（供 create/update 返回）。"""
    lines = [f"- {c['start']}{' ~ ' + c['end'] if c['end'] else ''} {c['title']}"
             for c in conflicts[:8]]
    return ("时间与以下已有日程冲突：\n" + "\n".join(lines)
            + "\n如果确认无碍，可传 ignore_conflicts=true 强制创建/更新。")


@register_tool(
    name="list_events",
    description=(
        "查询日历事件列表。date_from 为查询开始日期，date_to 为查询结束日期"
        "（均含当天；结束须晚于开始）。返回该时段内所有事件摘要，"
        "重复事件会自动展开每次发生并标注（重复）。"
        "适合回答“今天/本周/某段时间有什么安排”类问题。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "date_from": {"type": "string", "description": f"查询开始时间，{_TIME_DESC}"},
            "date_to": {"type": "string", "description": f"查询结束时间，{_TIME_DESC}，需晚于 date_from"},
        },
        "required": ["date_from", "date_to"],
    },
)
def list_events(date_from: str, date_to: str):
    tz = _tz()
    start = _parse(date_from, tz)
    end = _parse(date_to, tz)
    if end <= start:
        raise ValueError("date_to 必须晚于 date_from")

    rows: list[tuple] = []
    for ev in calendar_service.list_events(start, end):
        if ev.rrule:
            for occ in expand_rrule(ev.rrule, ev.start_utc, start, end, tz):
                if occ < end:  # 半开区间 [start, end)
                    rows.append((ev, occ))
        else:
            rows.append((ev, ev.start_utc))
    rows.sort(key=lambda pair: pair[1])

    result = []
    for ev, occ in rows[:30]:
        occ_end = None
        if ev.end_utc:
            dur = ev.end_utc - ev.start_utc
            occ_end = occ + dur
        title = ev.title + ("（重复）" if ev.rrule else "")
        result.append({
            "id": ev.id,
            "title": title,
            "start": fmt_dt(occ, tz),
            "end": fmt_dt(occ_end, tz) if occ_end else "",
            "all_day": ev.all_day,
        })
    if not result:
        return "该时段内没有日历事件"
    return result


@register_tool(
    name="create_event",
    description=(
        "创建日历事件。title 为标题（必填），start 为开始时间（必填，用户本地时区），"
        "end 为结束时间（可选，省略表示无结束时间）；all_day=true 表示全天事件"
        "（时分被忽略）；repeat 可选 none/daily/weekly/monthly/yearly；"
        "reminder_minutes 为提前提醒分钟数（0 表示准时提醒，省略表示不提醒）；"
        "ignore_conflicts 为是否忽略时间冲突（默认 false：创建前自动检测冲突，"
        "有冲突时不创建并返回冲突列表与建议；确认无碍可传 true 强制创建）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "事件标题，必填"},
            "start": {"type": "string", "description": f"开始时间，{_TIME_DESC}"},
            "end": {"type": "string", "description": f"结束时间，{_TIME_DESC}，可选"},
            "all_day": {"type": "boolean", "description": "是否全天事件，默认 false"},
            "description": {"type": "string", "description": "事件描述，可选"},
            "repeat": {
                "type": "string",
                "enum": ["none", "daily", "weekly", "monthly", "yearly"],
                "description": "重复规则：none 不重复 / daily 每天 / weekly 每周 / monthly 每月 / yearly 每年，默认 none",
            },
            "reminder_minutes": {
                "type": "integer",
                "description": "提前提醒分钟数，0 表示准时，省略或不传表示不提醒",
            },
            "ignore_conflicts": {
                "type": "boolean",
                "description": "是否忽略时间冲突强制创建，默认 false",
            },
        },
        "required": ["title", "start"],
    },
)
def create_event(title: str, start: str, end: str | None = None, all_day: bool = False,
                 description: str = "", repeat: str = "none",
                 reminder_minutes: int | None = None, ignore_conflicts: bool = False):
    title = str(title or "").strip()
    if not title:
        raise ValueError("事件标题不能为空")
    tz = _tz()
    all_day = bool(all_day)
    start_naive = _parse(start, tz, all_day)
    end_naive = _parse(end, tz, all_day) if end else None
    if end_naive is None and all_day:
        end_naive = start_naive + timedelta(days=1)
    if end_naive is not None:
        if end_naive < start_naive:
            raise ValueError("结束时间必须晚于开始时间")
        if end_naive == start_naive:
            if all_day:
                end_naive = start_naive + timedelta(days=1)
            else:
                raise ValueError("结束时间必须晚于开始时间")
    rrule = _repeat_rrule(repeat)
    reminder = _reminder(reminder_minutes)

    if not ignore_conflicts:
        conflicts = _find_conflicts(start_naive, end_naive)
        if conflicts:
            return _conflict_msg(conflicts)

    ev = calendar_service.create_event(
        title=title,
        start_naive=start_naive,
        end_naive=end_naive,
        all_day=all_day,
        description=str(description or ""),
        rrule=rrule,
        reminder_minutes=reminder,
        location="",
    )
    return f"已创建：{ev.title}（{fmt_dt(ev.start_utc, tz)}）"


@register_tool(
    name="update_event",
    description=(
        "更新日历事件。event_id 必填；其余参数只修改传入的字段，"
        "未传入的字段保持不变；repeat 传 none 表示取消重复；"
        "reminder_minutes 传 0 表示准时提醒，传 null 表示不修改提醒设置；"
        "ignore_conflicts 为是否忽略时间冲突（默认 false：改动时间时自动检测冲突，"
        "有冲突时不更新并返回冲突列表；确认无碍可传 true 强制更新）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "event_id": {"type": "integer", "description": "要更新的事件 ID"},
            "title": {"type": "string", "description": "新标题"},
            "start": {"type": "string", "description": f"新开始时间，{_TIME_DESC}"},
            "end": {"type": "string", "description": f"新结束时间，{_TIME_DESC}；传空串可清除结束时间"},
            "all_day": {"type": "boolean", "description": "是否全天事件"},
            "description": {"type": "string", "description": "新描述"},
            "repeat": {
                "type": "string",
                "enum": ["none", "daily", "weekly", "monthly", "yearly"],
                "description": "新重复规则；none 表示取消重复",
            },
            "reminder_minutes": {
                "type": "integer",
                "description": "新提醒提前分钟数；0 表示准时；不传表示不修改",
            },
            "ignore_conflicts": {
                "type": "boolean",
                "description": "是否忽略时间冲突强制更新，默认 false",
            },
        },
        "required": ["event_id"],
    },
)
def update_event(event_id: int, title: str | None = None, start: str | None = None,
                 end: str | None = None, all_day: bool | None = None,
                 description: str | None = None, repeat: str | None = None,
                 reminder_minutes: int | None = None, ignore_conflicts: bool = False):
    ev = calendar_service.get_event(event_id)
    if ev is None:
        raise ValueError(f"事件不存在：id={event_id}")

    tz = _tz()
    fields: dict = {}
    if title is not None:
        title = str(title).strip()
        if not title:
            raise ValueError("标题不能为空")
        fields["title"] = title
    if all_day is not None:
        fields["all_day"] = bool(all_day)
    all_day_eff = fields.get("all_day", ev.all_day)

    eff_start = ev.start_utc
    if start is not None:
        eff_start = _parse(start, tz, all_day_eff)
        fields["start_utc"] = eff_start
    eff_end = ev.end_utc
    if end is not None:
        if str(end).strip() == "":
            fields["end_utc"] = None
            eff_end = None
        else:
            eff_end = _parse(end, tz, all_day_eff)
            fields["end_utc"] = eff_end
    if eff_end is not None:
        if eff_end < eff_start:
            raise ValueError("结束时间必须晚于开始时间")
        if eff_end == eff_start:
            if all_day_eff:
                fields["end_utc"] = eff_start + timedelta(days=1)
            else:
                raise ValueError("结束时间必须晚于开始时间")

    if repeat is not None:
        fields["rrule"] = _repeat_rrule(repeat)
    if reminder_minutes is not None:
        fields["reminder_minutes"] = _reminder(reminder_minutes)
    if description is not None:
        fields["description"] = str(description)

    if not ignore_conflicts:
        eff_start = fields.get("start_utc", ev.start_utc)
        eff_end = fields.get("end_utc", ev.end_utc)
        conflicts = _find_conflicts(eff_start, eff_end, exclude_id=ev.id)
        if conflicts:
            return _conflict_msg(conflicts)

    calendar_service.update_event(ev, **fields)
    return f"已更新：{ev.title}"


@register_tool(
    name="delete_event",
    description="删除日历事件（软删除）。event_id 为要删除的事件 ID，删除后无法恢复。",
    dangerous=True,
    parameters={
        "type": "object",
        "properties": {
            "event_id": {"type": "integer", "description": "要删除的事件 ID"},
        },
        "required": ["event_id"],
    },
)
def delete_event(event_id: int):
    ev = calendar_service.get_event(event_id)
    if ev is None:
        raise ValueError(f"事件不存在：id={event_id}")
    title = ev.title
    calendar_service.soft_delete_event(ev)
    return f"已删除：{title}"
