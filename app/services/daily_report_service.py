"""Archive briefings by civil date and project them into the calendar.

Reports are not scheduling events: they neither reserve time nor become input
for tomorrow's briefing. Each section is updated atomically, so concurrent
morning/noon/evening runs cannot create duplicates or overwrite other sections.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import url_for
from sqlalchemy import case, or_, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.extensions import db
from app.models.conversation import Conversation, Message
from app.models.daily_report import DailyReport
from app.utils.timeutil import fmt_dt, user_tz, utcnow

KINDS = {
    "morning": ("早安简报", "☀️ 早安简报 "),
    "noon": ("午间简报", "🕛 午间简报 "),
    "evening": ("晚间复盘", "🌙 晚间复盘 "),
}
MORNING_GUIDE = ("早上好！今日安排已整理好（见上方简报）。"
                 "需要我帮你排优先级、调整日程或补充提醒吗？")


def record_briefing(user, kind: str, report_date: date, content: str,
                    conversation_id: int | None, generated_at: datetime | None = None,
                    *, missing_only: bool = False) -> None:
    """Upsert one section in the caller's transaction; do not send notifications."""
    if kind not in KINDS or type(report_date) is not date:
        raise ValueError("日报类型或日期无效")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("日报内容不能为空")
    generated_at = generated_at or utcnow()
    body_key, conversation_key, time_key = (kind + suffix for suffix in
                                           ("_content", "_conversation_id", "_generated_at"))
    table = DailyReport.__table__
    values = {"user_id": user.id, "report_date": report_date, "timezone": str(user_tz(user)),
              body_key: content, conversation_key: conversation_id, time_key: generated_at,
              "updated_at": utcnow()}
    dialect = db.engine.dialect.name
    existing_time = table.c[time_key]
    condition = table.c[body_key] == "" if missing_only else or_(
        existing_time.is_(None), existing_time <= generated_at)
    # Keep the timestamp assignment last: MySQL evaluates UPDATE assignments
    # left to right, and every condition must compare the previous timestamp.
    keys = ([conversation_key, "updated_at", time_key, body_key] if missing_only
            else [body_key, conversation_key, "updated_at", time_key])
    if dialect == "mysql":
        statement = mysql_insert(table).values(**values)
        statement = statement.on_duplicate_key_update([
            (key, case((condition, values[key]), else_=table.c[key])) for key in keys
        ])
    elif dialect == "sqlite":
        statement = sqlite_insert(table).values(**values).on_conflict_do_update(
            index_elements=[table.c.user_id, table.c.report_date],
            set_={key: values[key] for key in keys}, where=condition)
    else:
        raise RuntimeError("日报归档需要 MySQL 或 SQLite")
    db.session.execute(statement)


def _check_range(start_date, end_date):
    if (type(start_date) is not date or type(end_date) is not date
            or start_date > end_date or (end_date - start_date).days > 365):
        raise ValueError("日报日期范围须在一年内，结束日期不能早于开始日期")


def backfill_reports(user, start_date: date, end_date: date) -> int:
    """Import missing historical sections in this inclusive date window.

    Only the generated portion before the first user reply is eligible. A chat
    continuation or the morning greeting must never become the report body.
    Repeated reads never replace already archived sections or send messages.
    """
    _check_range(start_date, end_date)
    titles = {}
    for offset in range((end_date - start_date).days + 1):
        day = start_date + timedelta(days=offset)
        for kind, (_, prefix) in KINDS.items():
            titles[prefix + day.isoformat()] = (day, kind)
    conversations = db.session.execute(select(Conversation.id, Conversation.title).where(
        Conversation.user_id == user.id, Conversation.title.in_(titles))).all()
    if not conversations:
        return 0
    existing = list_reports(user, start_date, end_date, backfill=False)
    present = {(row.report_date, kind) for row in existing for kind in KINDS
               if getattr(row, kind + "_content")}
    candidates = {cid: titles[title] for cid, title in conversations
                  if titles[title] not in present}
    if not candidates:
        return 0
    latest, stopped = {}, set()
    rows = db.session.execute(select(Message).where(
        Message.conversation_id.in_(candidates)).order_by(Message.id).execution_options(yield_per=100))
    for message in rows.scalars():
        cid = message.conversation_id
        if cid in stopped:
            continue
        if message.role == "user":
            stopped.add(cid)
        elif (message.role == "assistant" and not message.tool_calls
              and message.content and message.content.strip()
              and message.content.strip() != MORNING_GUIDE):
            key = candidates[cid]
            value = (message.created_at or datetime.min, message.id, cid, message.content)
            if key not in latest or value[:2] > latest[key][:2]:
                latest[key] = value
    for (day, kind), (created_at, _, cid, content) in latest.items():
        record_briefing(user, kind, day, content, cid, created_at, missing_only=True)
    if latest:
        db.session.commit()
    return len(latest)


def list_reports(user, start_date: date, end_date: date, *, backfill: bool = True) -> list[DailyReport]:
    _check_range(start_date, end_date)
    if backfill:
        backfill_reports(user, start_date, end_date)
    return DailyReport.query.filter(
        DailyReport.user_id == user.id, DailyReport.report_date >= start_date,
        DailyReport.report_date <= end_date).order_by(DailyReport.report_date).populate_existing().all()


def backfill_history(user) -> int:
    """Explicit upgrade helper: import only months that contain legacy reports."""
    import calendar

    months = set()
    titles = db.session.scalars(select(Conversation.title).where(
        Conversation.user_id == user.id,
        or_(*(Conversation.title.startswith(prefix) for _, prefix in KINDS.values()))))
    for title in titles:
        for _, prefix in KINDS.values():
            if not title.startswith(prefix):
                continue
            try:
                day = date.fromisoformat(title[len(prefix):])
                if title == prefix + day.isoformat():
                    months.add((day.year, day.month))
            except ValueError:
                pass
    return sum(backfill_reports(user, date(year, month, 1),
                               date(year, month, calendar.monthrange(year, month)[1]))
               for year, month in sorted(months))


def calendar_items(user, start_date: date, end_date: date) -> list[dict]:
    """Read-only all-day projections, shared by browser and external clients."""
    reports = list_reports(user, start_date, end_date)
    conversation_ids = {getattr(row, kind + "_conversation_id") for row in reports for kind in KINDS}
    conversation_ids.discard(None)
    owned = set(db.session.scalars(select(Conversation.id).where(
        Conversation.user_id == user.id, Conversation.id.in_(conversation_ids)))) if conversation_ids else set()
    items = []
    for row in reports:
        sections = []
        for kind, (label, _) in KINDS.items():
            content = getattr(row, kind + "_content")
            if not content:
                continue
            cid = getattr(row, kind + "_conversation_id")
            sections.append({"kind": kind, "label": label, "content": content,
                             "generated_at": fmt_dt(getattr(row, kind + "_generated_at"), user_tz(user)),
                             "conversation_url": url_for("chat.index", conversation=cid) if cid in owned else ""})
        day = row.report_date.isoformat()
        end = (row.report_date + timedelta(days=1)).isoformat() if row.report_date < date.max else ""
        items.append({"id": f"daily-report-{row.id}", "report_id": row.id, "kind": "daily_report",
                      "title": f"日报 {day}", "date": day, "start": day, "end": end,
                      "all_day": True, "readonly": True, "recurring": False, "rrule": "",
                      "reminder_minutes": None, "location": "", "sections": sections,
                      "description": "\n\n".join(f"## {part['label']}\n\n{part['content']}" for part in sections)})
    return items
