"""Account-isolated calendar and task API for external clients."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from itertools import islice

from dateutil.rrule import rrulestr
from flask import Blueprint, g, request
from sqlalchemy import or_

from app.extensions import csrf, db
from app.models.event import Event
from app.models.task import TASK_STATUSES, Task
from app.services import calendar_service, task_service
from app.utils.integration_api import (
    ApiError, api_authenticated, iso_datetime, json_body, pagination,
    parse_datetime, register_api_errors, success,
)
from app.utils.timeutil import to_naive_utc, user_tz, utcnow

bp = Blueprint("integration_data", __name__, url_prefix="/api/v1")
csrf.exempt(bp)
register_api_errors(bp)

_TASK_FIELDS = {"title", "notes", "due_at", "priority", "status", "project", "tags"}
_EVENT_FIELDS = {
    "title", "description", "location", "start_at", "end_at", "all_day", "rrule",
    "reminder_minutes",
}
_WEEKDAYS = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}
_MAX_OCCURRENCES = 1000
_MAX_CANDIDATES = 1000
_MAX_ITERATIONS = 10000


def _rule_periods(rule_text, start, end):
    """Bound internal recurrence scanning, including intervals with no matches."""
    values = dict(part.split("=", 1) for part in rule_text.split(";"))
    interval = int(values.get("INTERVAL", "1"))
    if "UNTIL" in values:
        end = min(end, datetime.strptime(values["UNTIL"], "%Y%m%dT%H%M%SZ"))
    days = max(0, (end - start).days)
    periods = {
        "DAILY": days,
        "WEEKLY": days // 7,
        "MONTHLY": max(0, (end.year - start.year) * 12 + end.month - start.month),
        "YEARLY": max(0, end.year - start.year),
    }[values["FREQ"]]
    return periods // interval + 2


def _unknown(fields, allowed):
    unexpected = set(fields) - allowed
    if unexpected:
        raise ApiError("未知字段：" + ", ".join(sorted(unexpected)))


def _query(allowed):
    _unknown(request.args, allowed)
    if any(len(request.args.getlist(k)) != 1 for k in request.args):
        raise ApiError("查询参数不能重复")


def _text(value, name, maximum, *, nonempty=False):
    if not isinstance(value, str) or len(value) > maximum:
        raise ApiError(f"{name} 必须是最多 {maximum} 字的字符串")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ApiError(f"{name} 包含无效的 Unicode 字符") from exc
    value = value.strip() if nonempty else value
    if nonempty and not value:
        raise ApiError(f"{name} 不能为空")
    return value


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ApiError(f"{name} 必须是 {minimum} 至 {maximum} 的整数")
    return value


def _range(start_name, end_name, *, required=False):
    start = parse_datetime(request.args[start_name], start_name) if start_name in request.args else None
    end = parse_datetime(request.args[end_name], end_name) if end_name in request.args else None
    if required and (start is None or end is None):
        raise ApiError(f"需要 {start_name} 和 {end_name}")
    if start is not None and end is not None and start >= end:
        raise ApiError(f"{end_name} 必须晚于 {start_name}")
    return start, end


def _find(model, resource_id):
    if not 1 <= resource_id <= 2147483647:
        raise ApiError("资源不存在", status=404, code="not_found")
    row = model.query.filter(
        model.user_id == g.api_user.id, model.id == resource_id, model.deleted_at.is_(None),
    ).first()
    if row is None:
        raise ApiError("资源不存在", status=404, code="not_found")
    return row


def _task_view(task):
    return {
        "id": task.id, "title": task.title, "notes": task.notes,
        "due_at": iso_datetime(task.due_utc), "priority": task.priority,
        "status": task.status, "project": task.project, "tags": task.tags,
        "completed_at": iso_datetime(task.completed_at),
        "created_at": iso_datetime(task.created_at), "updated_at": iso_datetime(task.updated_at),
    }


def _event_view(event):
    return {
        "id": event.id, "title": event.title, "description": event.description,
        "location": event.location, "start_at": iso_datetime(event.start_utc),
        "end_at": iso_datetime(event.end_utc), "all_day": event.all_day,
        "rrule": event.rrule, "reminder_minutes": event.reminder_minutes,
        "created_at": iso_datetime(event.created_at), "updated_at": iso_datetime(event.updated_at),
    }


@bp.get("/daily-reports")
@api_authenticated
def daily_reports():
    """Read the same daily archives shown in the user's calendar."""
    from app.services.daily_report_service import calendar_items

    _query({"date_from", "date_to"})
    try:
        raw_start, raw_end = request.args.get("date_from", ""), request.args.get("date_to", "")
        start, end = date.fromisoformat(raw_start), date.fromisoformat(raw_end)
        if (start.isoformat() != raw_start or end.isoformat() != raw_end
                or start > end or (end - start).days > 365):
            raise ValueError()
    except (TypeError, ValueError):
        raise ApiError("请提供 YYYY-MM-DD 格式的 date_from 和 date_to，含首尾最多 366 天") from None
    return success(calendar_items(g.api_user, start, end))


def _task_fields(body, *, creating=False):
    _unknown(body, _TASK_FIELDS)
    if not body or (creating and "title" not in body):
        raise ApiError("需要提供 title" if creating else "更新内容不能为空")
    fields = {}
    for key, maximum in (("title", 255), ("notes", 20000), ("project", 64)):
        if key in body:
            fields[key] = _text(body[key], key, maximum, nonempty=key == "title")
    if "due_at" in body:
        fields["due_utc"] = parse_datetime(body["due_at"], "due_at", nullable=True)
    if "priority" in body:
        fields["priority"] = _integer(body["priority"], "priority", 1, 3)
    if "status" in body:
        if not isinstance(body["status"], str) or body["status"] not in TASK_STATUSES:
            raise ApiError("status 必须是 open、done 或 cancelled")
        fields["status"] = body["status"]
    if "tags" in body:
        if not isinstance(body["tags"], list) or len(body["tags"]) > 20:
            raise ApiError("tags 必须是最多 20 项的字符串数组")
        tags = [_text(t, "tags", 64, nonempty=True) for t in body["tags"]]
        fields["tags"] = list(dict.fromkeys(tags))
    return fields


def _validated_rrule(value, start):
    """A bounded subset: at most one occurrence per day, no rule sets or subdaily rules."""
    value = _text(value, "rrule", 255).strip().upper()
    if not value:
        return ""
    parts = value.split(";")
    if any(part.count("=") != 1 for part in parts):
        raise ApiError("rrule 格式不正确")
    pairs = [part.split("=", 1) for part in parts]
    rule = dict(pairs)
    if len(rule) != len(pairs):
        raise ApiError("rrule 参数不能重复")
    _unknown(rule, {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "BYMONTH", "WKST"})
    if rule.get("FREQ") not in {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}:
        raise ApiError("rrule 仅支持 DAILY、WEEKLY、MONTHLY、YEARLY")
    for name, maximum in (("INTERVAL", 366), ("COUNT", 10000)):
        if name in rule and (not re.fullmatch(r"[1-9]\d{0,4}", rule[name]) or int(rule[name]) > maximum):
            raise ApiError(f"rrule {name} 必须是 1 至 {maximum} 的整数")
    if "COUNT" in rule and "UNTIL" in rule:
        raise ApiError("rrule COUNT 与 UNTIL 不能同时使用")
    if "UNTIL" in rule:
        try:
            datetime.strptime(rule["UNTIL"], "%Y%m%dT%H%M%SZ")
        except ValueError as exc:
            raise ApiError("rrule UNTIL 必须是 UTC 日期时间，如 20261231T235959Z") from exc
    if "WKST" in rule and rule["WKST"] not in _WEEKDAYS:
        raise ApiError("rrule WKST 必须是 MO 至 SU")
    if "BYDAY" in rule:
        days = rule["BYDAY"].split(",")
        if len(days) > 7 or any(not re.fullmatch(r"(?:[+-]?[1-5])?(?:MO|TU|WE|TH|FR|SA|SU)", day) for day in days):
            raise ApiError("rrule BYDAY 格式不正确或超过 7 项")
        if rule["FREQ"] in {"DAILY", "WEEKLY"} and any(day not in _WEEKDAYS for day in days):
            raise ApiError("日、周重复的 BYDAY 不能带序号")
    for name, maximum, allow_negative in (("BYMONTH", 12, False), ("BYMONTHDAY", 31, True)):
        if name in rule:
            values = rule[name].split(",")
            pattern = r"-?[1-9]\d?" if allow_negative else r"[1-9]\d?"
            if len(values) > maximum or any(not re.fullmatch(pattern, v) or abs(int(v)) > maximum for v in values):
                raise ApiError(f"rrule {name} 格式不正确")
    # Impossible month/day combinations can force dateutil to scan to year 9999.
    if "BYMONTHDAY" in rule and "BYMONTH" in rule:
        month_days = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
        if not any(abs(int(day)) <= month_days[int(month)] for day in rule["BYMONTHDAY"].split(",") for month in rule["BYMONTH"].split(",")):
            raise ApiError("rrule 月份和日期组合永远不会发生")
    try:
        rrulestr(value, dtstart=start.replace(tzinfo=timezone.utc).astimezone(user_tz(g.api_user)))
    except (ValueError, TypeError, OverflowError) as exc:
        raise ApiError("rrule 格式不正确") from exc
    return value


def _event_fields(body, event=None):
    _unknown(body, _EVENT_FIELDS)
    if not body:
        raise ApiError("更新内容不能为空")
    if event is None and not {"title", "start_at"}.issubset(body):
        raise ApiError("需要提供 title 和 start_at")
    fields = {}
    for key, maximum in (("title", 255), ("description", 20000), ("location", 255)):
        if key in body:
            fields[key] = _text(body[key], key, maximum, nonempty=key == "title")
    if "start_at" in body:
        fields["start_utc"] = parse_datetime(body["start_at"], "start_at")
    if "end_at" in body:
        fields["end_utc"] = parse_datetime(body["end_at"], "end_at", nullable=True)
    start = fields.get("start_utc", event.start_utc if event else None)
    end = fields.get("end_utc", event.end_utc if event else None)
    if end is not None and end <= start:
        raise ApiError("end_at 必须晚于 start_at")
    if "all_day" in body:
        if type(body["all_day"]) is not bool:
            raise ApiError("all_day 必须是布尔值")
        fields["all_day"] = body["all_day"]
    if "reminder_minutes" in body:
        value = body["reminder_minutes"]
        fields["reminder_minutes"] = None if value is None else _integer(value, "reminder_minutes", 0, 525600)
    if "rrule" in body:
        fields["rrule"] = _validated_rrule(body["rrule"], start)
    elif "start_utc" in fields and event and event.rrule:
        _validated_rrule(event.rrule, start)
    return fields


@bp.get("/tasks")
@api_authenticated
def list_tasks():
    _query({"limit", "offset", "status", "q", "project", "due_after", "due_before"})
    limit, offset = pagination()
    start, end = _range("due_after", "due_before")
    query = Task.query.filter(Task.user_id == g.api_user.id, Task.deleted_at.is_(None))
    if "status" in request.args:
        if request.args["status"] not in TASK_STATUSES:
            raise ApiError("status 必须是 open、done 或 cancelled")
        query = query.filter(Task.status == request.args["status"])
    if "project" in request.args:
        query = query.filter(Task.project == _text(request.args["project"], "project", 64))
    if "q" in request.args:
        term = _text(request.args["q"], "q", 255)
        query = query.filter(or_(Task.title.contains(term, autoescape=True), Task.notes.contains(term, autoescape=True)))
    if start is not None:
        query = query.filter(Task.due_utc >= start)
    if end is not None:
        query = query.filter(Task.due_utc < end)
    total = query.count()
    rows = query.order_by(Task.due_utc.is_(None), Task.due_utc, Task.priority.desc(), Task.id).offset(offset).limit(limit).all()
    return success([_task_view(t) for t in rows], pagination={"limit": limit, "offset": offset, "total": total})


@bp.post("/tasks")
@api_authenticated
def create_task():
    fields = _task_fields(json_body(), creating=True)
    task = Task(user_id=g.api_user.id, **fields)
    if fields.get("status") == "done":
        task.completed_at = utcnow()
    db.session.add(task)
    db.session.commit()
    return success(_task_view(task), status=201)


@bp.get("/tasks/<int:resource_id>")
@api_authenticated
def get_task(resource_id):
    return success(_task_view(_find(Task, resource_id)))


@bp.patch("/tasks/<int:resource_id>")
@api_authenticated
def update_task(resource_id):
    task = _find(Task, resource_id)
    fields = _task_fields(json_body())
    if "status" in fields:
        task.status = fields.pop("status")
        task.completed_at = utcnow() if task.status == "done" else None
    task_service.update_task(task, **fields)
    return success(_task_view(task))


@bp.delete("/tasks/<int:resource_id>")
@api_authenticated
def delete_task(resource_id):
    task = _find(Task, resource_id)
    task_service.soft_delete_task(task)
    return success({"id": task.id, "deleted": True})


def _overlap(query, start, end):
    if end is not None:
        query = query.filter(Event.start_utc < end)
    if start is not None:
        query = query.filter(or_(
            (Event.end_utc.is_(None)) & (Event.start_utc >= start),
            (Event.end_utc.isnot(None)) & (Event.end_utc > start),
        ))
    return query


@bp.get("/events")
@api_authenticated
def list_events():
    _query({"limit", "offset", "start", "end", "q"})
    limit, offset = pagination()
    start, end = _range("start", "end")
    query = Event.query.filter(Event.user_id == g.api_user.id, Event.deleted_at.is_(None))
    query = _overlap(query, start, end)
    if "q" in request.args:
        term = _text(request.args["q"], "q", 255)
        query = query.filter(Event.title.contains(term, autoescape=True))
    total = query.count()
    rows = query.order_by(Event.start_utc, Event.id).offset(offset).limit(limit).all()
    return success([_event_view(e) for e in rows], pagination={"limit": limit, "offset": offset, "total": total})


@bp.post("/events")
@api_authenticated
def create_event():
    fields = _event_fields(json_body())
    fields["start_naive"] = fields.pop("start_utc")
    if "end_utc" in fields:
        fields["end_naive"] = fields.pop("end_utc")
    event = calendar_service.create_event(g.api_user.id, **fields)
    return success(_event_view(event), status=201)


@bp.get("/events/<int:resource_id>")
@api_authenticated
def get_event(resource_id):
    return success(_event_view(_find(Event, resource_id)))


@bp.patch("/events/<int:resource_id>")
@api_authenticated
def update_event(resource_id):
    event = _find(Event, resource_id)
    fields = _event_fields(json_body(), event)
    calendar_service.update_event(event, **fields)
    return success(_event_view(event))


@bp.delete("/events/<int:resource_id>")
@api_authenticated
def delete_event(resource_id):
    event = _find(Event, resource_id)
    calendar_service.soft_delete_event(event)
    return success({"id": event.id, "deleted": True})


@bp.get("/events/occurrences")
@api_authenticated
def event_occurrences():
    _query({"start", "end"})
    start, end = _range("start", "end", required=True)
    if end - start > timedelta(days=93):
        raise ApiError("日历展开范围不能超过 93 天")
    query = Event.query.filter(
        Event.user_id == g.api_user.id, Event.deleted_at.is_(None), Event.start_utc < end,
        or_(Event.rrule != "", Event.end_utc > start,
            Event.end_utc.is_(None) & (Event.start_utc >= start)),
    )
    rows = query.order_by(Event.start_utc, Event.id).limit(_MAX_CANDIDATES + 1).all()
    if len(rows) > _MAX_CANDIDATES:
        raise ApiError("候选日程过多，请缩小查询范围", code="range_too_complex")
    result = []
    iterations = 0
    periods = 0
    for event in rows:
        duration = event.end_utc - event.start_utc if event.end_utc else None
        if event.rrule:
            rule_text = _validated_rrule(event.rrule, event.start_utc)
            periods += _rule_periods(rule_text, event.start_utc, end)
            if periods > _MAX_ITERATIONS:
                raise ApiError("重复规则历史扫描量过大，请调整重复规则起点或缩小范围", code="range_too_complex")
            # UNTIL bounds even sparse legacy rules; never allow internal scanning beyond this request.
            local_start = event.start_utc.replace(tzinfo=timezone.utc).astimezone(user_tz(g.api_user))
            rule = rrulestr(rule_text, dtstart=local_start)
            bound = end.replace(tzinfo=timezone.utc)
            values = dict(part.split("=", 1) for part in rule_text.split(";"))
            if "UNTIL" in values:
                bound = min(bound, datetime.strptime(values["UNTIL"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc))
            # Cap time internally and apply COUNT externally; the library rejects their combination.
            rule = rule.replace(until=bound, count=None)
            occurrence_times = islice(rule, int(values["COUNT"])) if "COUNT" in values else rule
        else:
            occurrence_times = (event.start_utc,)
        for occurrence in occurrence_times:
            iterations += 1
            if iterations > _MAX_ITERATIONS:
                raise ApiError("重复规则展开量过大，请缩小范围或调整重复规则", code="range_too_complex")
            occurrence = to_naive_utc(occurrence)
            if occurrence >= end:
                break
            try:
                occurrence_end = occurrence + duration if duration else None
            except OverflowError as exc:
                raise ApiError("重复日程结束时间超出支持的日期范围") from exc
            if (occurrence_end <= start if occurrence_end else occurrence < start):
                continue
            item = _event_view(event)
            item.update(event_id=event.id, occurrence_id=f"{event.id}:{iso_datetime(occurrence)}",
                        start_at=iso_datetime(occurrence), end_at=iso_datetime(occurrence_end))
            result.append(item)
            if len(result) > _MAX_OCCURRENCES:
                raise ApiError("发生次数超过 1000，请缩小查询范围", code="range_too_complex")
    result.sort(key=lambda item: (item["start_at"], item["event_id"]))
    return success(result, range={"start": iso_datetime(start), "end": iso_datetime(end)},
                   pagination={"limit": _MAX_OCCURRENCES, "offset": 0, "total": len(result)})
