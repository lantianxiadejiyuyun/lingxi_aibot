"""日历蓝图：月视图 / 当日明细 / JSON API（创建、更新、软删除、取事件）。

- 页面路由均 @login_required，主页面函数名 index。
- JSON 接口 @csrf.exempt + @login_required，返回 {"ok": ...}。
- 时间：表单/JSON 收到的为用户时区字符串 → parse_local 转 naive UTC；
  JSON 返回用 fmt_dt 转好的用户时区字符串。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from app.extensions import csrf
from app.models.event import Event
from app.services import calendar_service
from app.utils.timeutil import (
    day_bounds, expand_rrule, fmt_dt, parse_local, to_naive_utc, to_user, user_tz,
)

bp = Blueprint("calendar", __name__, url_prefix="/calendar")

WEEKDAYS_CN = ["一", "二", "三", "四", "五", "六", "日"]
REPEAT_MAP = calendar_service.REPEAT_MAP


# ---------------------------------------------------------------- 工具函数

def _fail(message: str, code: int = 400):
    return jsonify({"ok": False, "error": message}), code


def _parse_ts(raw, tz, all_day: bool) -> datetime | None:
    """用户时区时间字符串 → naive UTC；all_day 时归一到当天 00:00（用户时区）。"""
    text = str(raw).strip()
    dt = parse_local(text, tz)
    if dt is None:
        return None
    if all_day:
        local = to_user(dt, tz)
        dt = to_naive_utc(datetime.combine(local.date(), time.min, tzinfo=tz))
    return dt


def _reminder(raw) -> tuple[int | None, bool]:
    """解析提醒分钟数。返回 (值, 是否合法)；空/None → (None, True)。"""
    if raw is None or raw == "":
        return None, True
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return None, False
    if val < 0:
        return None, False
    return val, True


def _grid_dates(year: int, month: int) -> tuple[date, date]:
    """月视图网格（周一起始）的首日与末日（本地日期）。"""
    first = date(year, month, 1)
    grid_start = first - timedelta(days=first.weekday())
    if month == 12:
        next_first = date(year + 1, 1, 1)
    else:
        next_first = date(year, month + 1, 1)
    last = next_first - timedelta(days=1)
    grid_end = last + timedelta(days=6 - last.weekday())
    return grid_start, grid_end


def _occ_item(ev: Event, occ: datetime, tz, grid_start: date, grid_end: date,
              recurring: bool) -> dict:
    """把一次发生转为 API/模板用的 dict。

    date 为用于网格分桶的本地日期（跨网格起点时钳到网格起点）；
    start/end 为用户时区格式化字符串。
    """
    dur = (ev.end_utc - ev.start_utc) if ev.end_utc else None
    occ_end = occ + dur if dur else None
    start_local = to_user(occ, tz)
    bucket_date = max(start_local.date(), grid_start)
    start_str = fmt_dt(occ, tz) if not ev.all_day else fmt_dt(occ, tz, "%Y-%m-%d")
    end_str = ""
    if occ_end is not None:
        end_str = fmt_dt(occ_end, tz) if not ev.all_day else fmt_dt(occ_end, tz, "%Y-%m-%d")
    return {
        "id": ev.id,
        "title": ev.title,
        "start": start_str,
        "end": end_str,
        "all_day": ev.all_day,
        "rrule": ev.rrule,
        "reminder_minutes": ev.reminder_minutes,
        "location": ev.location,
        "description": ev.description,
        "recurring": recurring,
        "date": bucket_date.strftime("%Y-%m-%d"),
    }


def _month_occurrences(year: int, month: int, tz) -> list[dict]:
    """月视图网格范围内的所有事件发生（含重复展开），按日期/时间排序。"""
    gs, ge = _grid_dates(year, month)
    s_utc, _ = day_bounds(gs, tz)
    _, e_utc = day_bounds(ge, tz)
    events = calendar_service.list_events(s_utc, e_utc, current_user.id)
    items = []
    for ev in events:
        if ev.rrule:
            for occ in expand_rrule(ev.rrule, ev.start_utc, s_utc, e_utc, tz):
                occ_date = to_user(occ, tz).date()
                if gs <= occ_date <= ge:  # 排除恰好落在区间端点的发生
                    items.append(_occ_item(ev, occ, tz, gs, ge, True))
        else:
            items.append(_occ_item(ev, ev.start_utc, tz, gs, ge, False))
    items.sort(key=lambda it: (it["date"], it["start"]))
    return items


def _spread_dates(it: dict) -> list[str]:
    """事件覆盖的全部本地日期（含跨天），供网格/明细分桶。"""
    dates = [it["date"]]
    end = it.get("end") or ""
    if end and end[:10] > it["date"]:
        last = end[:10]
        # 结束于午夜（或全天日期串）→ 结束日不包含
        if " " not in end or end.endswith(" 00:00"):
            last = (date.fromisoformat(last) - timedelta(days=1)).strftime("%Y-%m-%d")
            if last < it["date"]:
                last = it["date"]
        cur = date.fromisoformat(it["date"])
        guard = 0
        while cur.strftime("%Y-%m-%d") < last and guard < 60:
            cur += timedelta(days=1)
            dates.append(cur.strftime("%Y-%m-%d"))
            guard += 1
    return dates


def _day_occurrences(day_str: str, tz) -> list[dict]:
    """某一天的全部事件发生（含跨天覆盖与重复展开）。"""
    try:
        d = date.fromisoformat(day_str)
    except (TypeError, ValueError):
        return []
    s_utc, e_utc = day_bounds(d, tz)
    events = calendar_service.list_events(s_utc, e_utc, current_user.id)
    items = []
    for ev in events:
        if ev.rrule:
            for occ in expand_rrule(ev.rrule, ev.start_utc, s_utc, e_utc, tz):
                if to_user(occ, tz).date() == d:  # 只取落在当天的发生
                    items.append(_occ_item(ev, occ, tz, d, d, True))
        else:
            start_d = to_user(ev.start_utc, tz).date()
            end_d = start_d
            if ev.end_utc:
                end_local = to_user(ev.end_utc, tz)
                end_d = end_local.date()
                if end_local.time() == time(0, 0) and end_d > start_d:
                    end_d -= timedelta(days=1)
            if start_d <= d <= end_d:
                items.append(_occ_item(ev, ev.start_utc, tz, d, d, False))
    items.sort(key=lambda it: it["start"])
    return items


# ---------------------------------------------------------------- 页面

@bp.route("/")
@login_required
def index():
    tz = user_tz(current_user)
    now = datetime.now(tz)

    try:
        year = int(request.args.get("year", now.year))
        month = int(request.args.get("month", now.month))
    except (TypeError, ValueError):
        year, month = now.year, now.month
    if not 1 <= month <= 12:
        year, month = now.year, now.month
    year = max(1900, min(9999, year))

    day_str = request.args.get("day") or now.strftime("%Y-%m-%d")
    try:
        date.fromisoformat(day_str)
    except (TypeError, ValueError):
        day_str = now.strftime("%Y-%m-%d")

    month_events = _month_occurrences(year, month, tz)
    day_events = _day_occurrences(day_str, tz)
    today = now.strftime("%Y-%m-%d")

    # 月网格分桶（含跨天 spread）
    by_date: dict[str, list[dict]] = {}
    for it in month_events:
        for d in _spread_dates(it):
            by_date.setdefault(d, []).append(it)

    cells = []
    gs, ge = _grid_dates(year, month)
    cur = gs
    while cur <= ge:
        key = cur.strftime("%Y-%m-%d")
        evs = sorted(by_date.get(key, []), key=lambda it: it["start"])
        shown = [{
            "id": it["id"],
            "time": "全天" if it["all_day"] else it["start"][11:16],
            "title": it["title"],
        } for it in evs[:3]]
        cells.append({
            "date": key,
            "day_num": cur.day,
            "out": cur.month != month,
            "today": key == today,
            "events": shown,
            "more": max(0, len(evs) - 3),
        })
        cur += timedelta(days=1)

    return render_template(
        "calendar/index.html",
        year=year, month=month, day=day_str, today=today,
        month_label=f"{year} 年 {month} 月",
        cells=cells, day_events=day_events, month_events=month_events,
    )


# ---------------------------------------------------------------- JSON API

@bp.route("/api/create", methods=["POST"])
@csrf.exempt
@login_required
def api_create():
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title") or "").strip()
    if not title:
        return _fail("标题不能为空")

    tz = user_tz(current_user)
    all_day = bool(payload.get("all_day"))

    start_str = payload.get("start")
    if not start_str:
        return _fail("开始时间不能为空")
    start_naive = _parse_ts(start_str, tz, all_day)
    if start_naive is None:
        return _fail("开始时间格式不正确")

    end_naive = None
    if payload.get("end") not in (None, ""):
        end_naive = _parse_ts(payload["end"], tz, all_day)
        if end_naive is None:
            return _fail("结束时间格式不正确")
    if end_naive is None and all_day:
        end_naive = start_naive + timedelta(days=1)
    if end_naive is not None:
        if end_naive < start_naive:
            return _fail("结束时间必须晚于开始时间")
        if end_naive == start_naive:
            if all_day:
                end_naive = start_naive + timedelta(days=1)
            else:
                return _fail("结束时间必须晚于开始时间")

    repeat = str(payload.get("repeat") or "none").strip()
    rrule = REPEAT_MAP.get(repeat)
    if rrule is None:
        return _fail("repeat 取值非法")

    reminder, ok = _reminder(payload.get("reminder_minutes"))
    if not ok:
        return _fail("提醒时间必须是整数")

    ev = calendar_service.create_event(
        current_user.id,
        title=title,
        start_naive=start_naive,
        end_naive=end_naive,
        all_day=all_day,
        description=str(payload.get("description") or ""),
        rrule=rrule,
        reminder_minutes=reminder,
        location=str(payload.get("location") or ""),
    )
    return jsonify({"ok": True, "data": {"id": ev.id}})


@bp.route("/api/update", methods=["POST"])
@csrf.exempt
@login_required
def api_update():
    payload = request.get_json(silent=True) or {}
    event_id = payload.get("event_id")
    ev = calendar_service.get_event(event_id, current_user.id)
    if ev is None:
        return _fail("事件不存在")

    tz = user_tz(current_user)
    fields: dict = {}

    # 标题
    if "title" in payload and payload["title"] is not None:
        title = str(payload["title"]).strip()
        if not title:
            return _fail("标题不能为空")
        fields["title"] = title

    # 全天
    if "all_day" in payload and payload["all_day"] is not None:
        fields["all_day"] = bool(payload["all_day"])
    all_day = fields.get("all_day", ev.all_day)

    # 开始（空串或 None 表示不修改）
    eff_start = ev.start_utc
    if "start" in payload and payload["start"] not in (None, ""):
        start_naive = _parse_ts(payload["start"], tz, all_day)
        if start_naive is None:
            return _fail("开始时间格式不正确")
        fields["start_utc"] = start_naive
        eff_start = start_naive

    # 结束：None 表示不修改；空串表示清空
    eff_end = ev.end_utc
    if "end" in payload and payload["end"] is not None:
        if str(payload["end"]).strip() == "":
            fields["end_utc"] = None
            eff_end = None
        else:
            end_naive = _parse_ts(payload["end"], tz, all_day)
            if end_naive is None:
                return _fail("结束时间格式不正确")
            fields["end_utc"] = end_naive
            eff_end = end_naive

    if eff_end is not None and eff_start is not None:
        if eff_end < eff_start:
            return _fail("结束时间必须晚于开始时间")
        if eff_end == eff_start:
            if all_day:
                fields["end_utc"] = eff_start + timedelta(days=1)
            else:
                return _fail("结束时间必须晚于开始时间")

    # 重复：none 表示取消重复
    if "repeat" in payload and payload["repeat"] is not None:
        repeat = str(payload["repeat"]).strip()
        rrule = REPEAT_MAP.get(repeat)
        if rrule is None:
            return _fail("repeat 取值非法")
        fields["rrule"] = rrule

    # 提醒：None 表示不修改；空串表示清除
    if "reminder_minutes" in payload and payload["reminder_minutes"] is not None:
        reminder, ok = _reminder(payload["reminder_minutes"])
        if not ok:
            return _fail("提醒时间必须是整数")
        fields["reminder_minutes"] = reminder

    if "description" in payload and payload["description"] is not None:
        fields["description"] = str(payload["description"])
    if "location" in payload and payload["location"] is not None:
        fields["location"] = str(payload["location"])

    calendar_service.update_event(ev, **fields)
    return jsonify({"ok": True, "data": {"id": ev.id}})


@bp.route("/api/delete", methods=["POST"])
@csrf.exempt
@login_required
def api_delete():
    payload = request.get_json(silent=True) or {}
    ev = calendar_service.get_event(payload.get("event_id"), current_user.id)
    if ev is None:
        return _fail("事件不存在")
    calendar_service.soft_delete_event(ev)
    return jsonify({"ok": True, "data": {"id": ev.id}})


@bp.route("/api/events", methods=["GET"])
@csrf.exempt
@login_required
def api_events():
    tz = user_tz(current_user)
    now = datetime.now(tz)
    try:
        year = int(request.args.get("year", now.year))
        month = int(request.args.get("month", now.month))
    except (TypeError, ValueError):
        year, month = now.year, now.month
    if not 1 <= month <= 12:
        year, month = now.year, now.month
    year = max(1900, min(9999, year))
    events = _month_occurrences(year, month, tz)
    return jsonify({"ok": True, "data": {"events": events}})
