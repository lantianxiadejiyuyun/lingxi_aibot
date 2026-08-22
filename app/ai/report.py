"""周报 / 月报：周期数据汇总 → LLM 生成 Markdown 报告 → 落库 → 调度动作推送。

- 周报 = 最近 7 天；月报 = 最近 30 天
- 数据：日程事件、完成任务、新建笔记/记忆/网页/图片/技能、逾期任务
- LLM 不可用时降级为纯文本 Markdown；报告复用当天同名会话（可回看）
- 调度线程无 current_user，时区取自 user.timezone
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.extensions import db
from app.models.conversation import Conversation, Message
from app.models.scheduled_job import ScheduledJob  # noqa: F401
from app.models.task import STATUS_DONE, STATUS_OPEN, Task
from app.models.user import User
from app.scheduler import register_action
from app.utils.timeutil import day_bounds, expand_rrule, fmt_dt, user_tz, utcnow

logger = logging.getLogger(__name__)

try:
    from app.services import calendar_service

    _HAS_CALENDAR = True
except ImportError:  # pragma: no cover
    calendar_service = None
    _HAS_CALENDAR = False

_PERIOD_DAYS = {"weekly": 7, "monthly": 30}
_KIND_NAME = {"weekly": "周报", "monthly": "月报"}


def _fmt(dt, tz) -> str:
    return fmt_dt(dt, tz, "%Y-%m-%d") if dt else ""


def _stats(kind: str, user) -> dict:
    """周期内数据统计。"""
    from app.models.image import ImageAsset
    from app.models.memory import Memory
    from app.models.note import Note
    from app.models.skill import Skill
    from app.models.webpage import WebPage

    tz = user_tz(user)
    today = datetime.now(tz).date()
    days = _PERIOD_DAYS[kind]
    start_naive = day_bounds(today - timedelta(days=days), tz)[0]
    end_naive = utcnow()

    # 日程事件（重复事件展开到周期内实际发生时间）
    event_lines: list[str] = []
    if _HAS_CALENDAR:
        events = calendar_service.list_events(start_naive, end_naive, user.id)
        for ev in events[:30]:
            if ev.rrule:
                occs = expand_rrule(ev.rrule, ev.start_utc, start_naive, end_naive, tz)
                for occ in occs[:5]:
                    event_lines.append(f"- {fmt_dt(occ, tz)} {ev.title}（重复）")
            else:
                event_lines.append(f"- {_fmt(ev.start_utc, tz)} {ev.title}")
    # 完成任务
    done_tasks = Task.query.filter(
        Task.status == STATUS_DONE, Task.deleted_at.is_(None), Task.user_id == user.id,
        Task.completed_at >= start_naive, Task.completed_at < end_naive,
    ).order_by(Task.completed_at).all()
    done_lines = [f"- {t.title}（{_fmt(t.completed_at, tz)}）" for t in done_tasks[:20]]
    # 逾期未完成（仅进行中的任务，已取消不计入）
    overdue = Task.query.filter(
        Task.status == STATUS_OPEN, Task.deleted_at.is_(None), Task.user_id == user.id,
        Task.due_utc.isnot(None), Task.due_utc < end_naive,
    ).count()
    # 笔记 / 记忆 / 网页 / 图片 / 技能
    notes = Note.query.filter(Note.deleted_at.is_(None), Note.user_id == user.id,
                              Note.created_at >= start_naive,
                              Note.created_at < end_naive).count()
    memories = Memory.query.filter(Memory.deleted_at.is_(None), Memory.user_id == user.id,
                                   Memory.created_at >= start_naive,
                                   Memory.created_at < end_naive).count()
    pages = WebPage.query.filter(WebPage.deleted_at.is_(None), WebPage.user_id == user.id,
                                 WebPage.created_at >= start_naive,
                                 WebPage.created_at < end_naive).count()
    images = ImageAsset.query.filter(ImageAsset.deleted_at.is_(None), ImageAsset.user_id == user.id,
                                     ImageAsset.created_at >= start_naive,
                                     ImageAsset.created_at < end_naive).count()
    skills = Skill.query.filter(Skill.deleted_at.is_(None),
                                Skill.created_at >= start_naive,
                                Skill.created_at < end_naive).count()
    return {
        "range": f"{_fmt(start_naive, tz)} ~ {_fmt(end_naive, tz)}",
        "events": event_lines or ["- 无"],
        "done_count": len(done_tasks),
        "done": done_lines or ["- 无"],
        "overdue": overdue,
        "notes": notes, "memories": memories,
        "pages": pages, "images": images, "skills": skills,
    }


def _fallback(kind: str, s: dict) -> str:
    """LLM 不可用时的纯文本 Markdown 降级。"""
    name = _KIND_NAME[kind]
    return (
        f"## {name}回顾（{s['range']}）\n\n"
        f"**完成 {s['done_count']} 项任务**（逾期 {s['overdue']} 项）\n\n"
        "### 日程事件\n" + "\n".join(s["events"]) + "\n\n"
        "### 完成任务\n" + "\n".join(s["done"]) + "\n\n"
        f"### 产出统计\n"
        f"- 新增笔记 {s['notes']} · 记忆 {s['memories']} · 网页 {s['pages']} · 图片 {s['images']} · 技能 {s['skills']}"
    )


def build_report(kind: str, user) -> str:
    """生成报告并存入同名会话，返回内容字符串。"""
    if kind not in _PERIOD_DAYS:
        raise ValueError(f"未知报告类型: {kind}")
    tz = user_tz(user)
    today = datetime.now(tz).date()
    stats = _stats(kind, user)

    from app.ai.llm import LLMClient, LLMError
    from app.ai.prompts import build_system_prompt, report_prompt

    content = ""
    llm = LLMClient()
    if llm.is_configured:
        try:
            content, _ = llm.chat([
                {"role": "system", "content": build_system_prompt(user)},
                {"role": "user", "content": report_prompt(kind, stats)},
            ])
        except LLMError as e:
            logger.warning("报告生成失败，降级为纯文本: %s", e)
            content = _fallback(kind, stats)
    else:
        content = _fallback(kind, stats)

    title = f"📊 {_KIND_NAME[kind]} {today.isoformat()}"
    conv = Conversation.query.filter(Conversation.title == title,
                                     Conversation.user_id == user.id).order_by(
        Conversation.id.desc()).first()
    if conv is None:
        conv = Conversation(title=title, user_id=user.id)
        db.session.add(conv)
    conv.updated_at = utcnow()
    conv.messages.append(Message(role="assistant", content=content, conversation=conv))
    db.session.commit()
    return content


@register_action("weekly_report", description="生成最近 7 天周报并推送（参数：channel 或 channels）")
def weekly(user, params: dict | None = None) -> None:
    from app.services.notify_service import notify_for

    params = params or {}
    content = build_report("weekly", user)
    notify_for("report", "📊 周报", content,
               user_id=user.id, explicit=params.get("channels") or params.get("channel"))


@register_action("monthly_report", description="生成最近 30 天月报并推送（参数：channel 或 channels）")
def monthly(user, params: dict | None = None) -> None:
    from app.services.notify_service import notify_for

    params = params or {}
    content = build_report("monthly", user)
    notify_for("report", "📊 月报", content,
               user_id=user.id, explicit=params.get("channels") or params.get("channel"))
