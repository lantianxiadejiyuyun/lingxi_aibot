"""早安简报 / 晚间复盘：数据汇总 → LLM 生成（或 Markdown 降级）→ 落库 → 调度动作推送。

调度线程无 current_user，时区一律取自 user.timezone。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.extensions import db
from app.models.conversation import Conversation, Message
from app.models.scheduled_job import ScheduledJob  # noqa: F401  保证模型已注册
from app.models.task import STATUS_DONE, Task
from app.models.user import User
from app.scheduler import register_action
from app.utils.timeutil import day_bounds, expand_rrule, fmt_dt, to_user, user_tz, utcnow, weekday_cn

logger = logging.getLogger(__name__)

# 依赖服务（其他模块并行开发中）：import 失败则跳过对应数据
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


def _expand_events(start_naive, end_naive, tz, user_id) -> list[tuple]:
    """区间内事件（含 rrule 展开），返回 [(Event, 发生时间 naive UTC)] 按时间排序。"""
    rows: list[tuple] = []
    if not _HAS_CALENDAR:
        return rows
    for ev in calendar_service.list_events(start_naive, end_naive, user_id):
        if ev.rrule:
            for occ in expand_rrule(ev.rrule, ev.start_utc, start_naive, end_naive, tz):
                rows.append((ev, occ))
        else:
            rows.append((ev, ev.start_utc))
    rows.sort(key=lambda pair: pair[1])
    return rows


def _open_tasks(user_id) -> list:
    if not _HAS_TASK:
        return []
    try:
        return task_service.list_tasks(user_id, status="open")
    except Exception:  # noqa: BLE001
        logger.exception("获取待办任务失败")
        return []


def _event_lines(rows: list[tuple], tz) -> list[str]:
    lines = []
    for ev, occ in rows:
        t = "全天" if ev.all_day else fmt_dt(occ, tz)
        suffix = "（重复）" if ev.rrule else ""
        lines.append(f"- {t} {ev.title}{suffix}")
    return lines or ["- 无"]


def _fallback_markdown(kind: str, data: dict) -> str:
    """LLM 不可用时的纯文本 Markdown 降级（标题 + 列表）。"""
    if kind == "morning":
        return (
            "## 今日日程\n" + "\n".join(data["today_events"]) + "\n\n"
            "## 待办任务\n" + "\n".join(data["open_tasks"]) + "\n\n"
            "## AI 建议\n- 保持节奏，优先处理今日到期的任务。"
        )
    if kind == "noon":
        return (
            "## 今日剩余日程\n" + "\n".join(data["today_events"]) + "\n\n"
            "## 今日已完成\n" + "\n".join(data["done_tasks"]) + "\n\n"
            "## 待办任务\n" + "\n".join(data["open_tasks"]) + "\n\n"
            "## 明日安排\n" + "\n".join(data["tomorrow_events"])
        )
    return (
        "## 今日完成\n" + "\n".join(data["done_tasks"]) + "\n\n"
        "## 未完成\n" + "\n".join(data["open_tasks"]) + "\n\n"
        "## 明日安排\n" + "\n".join(data["tomorrow_events"])
    )


def build_briefing(kind: str, user) -> str:
    """生成简报内容并存入会话（复用当天同名简报会话），返回内容字符串。"""
    if kind not in ("morning", "noon", "evening"):
        raise ValueError(f"未知简报类型: {kind}")

    tz = user_tz(user)
    today = datetime.now(tz).date()
    start, end = day_bounds(today, tz)
    yesterday_start, yesterday_end = day_bounds(today - timedelta(days=1), tz)
    tomorrow = today + timedelta(days=1)
    tomorrow_start, tomorrow_end = day_bounds(tomorrow, tz)

    # ---- 汇总数据 ----
    today_events = _expand_events(start, end, tz, user.id)
    tomorrow_events = _expand_events(tomorrow_start, tomorrow_end, tz, user.id)
    open_tasks = _open_tasks(user.id)

    if kind == "morning":
        done_yesterday = 0
        if _HAS_TASK:
            done_yesterday = Task.query.filter(
                Task.status == STATUS_DONE,
                Task.deleted_at.is_(None),
                Task.user_id == user.id,
                Task.completed_at >= yesterday_start,
                Task.completed_at < yesterday_end,
            ).count()
        today_event_lines = _event_lines(today_events, tz)
        open_task_lines = []
        for t in open_tasks[:15]:
            if t.due_utc is None:
                open_task_lines.append(f"- {t.title}")
            elif start <= t.due_utc < end:
                open_task_lines.append(f"- {t.title}（今日到期）")
            else:
                open_task_lines.append(f"- {t.title}（截止 {fmt_dt(t.due_utc, tz)}）")
        if not open_task_lines:
            open_task_lines = ["- 无"]
        summary = (
            f"今日日期：{today.isoformat()}（星期{weekday_cn(datetime.now(tz))}）\n"
            "今日事件：\n" + "\n".join(today_event_lines) + "\n"
            "待办任务：\n" + "\n".join(open_task_lines) + "\n"
            f"昨日完成任务数：{done_yesterday}"
        )
        title = f"☀️ 早安简报 {today.isoformat()}"
        data = {"today_events": today_event_lines, "open_tasks": open_task_lines}
    elif kind == "noon":
        done_today_tasks = []
        if _HAS_TASK:
            done_today_tasks = Task.query.filter(
                Task.status == STATUS_DONE,
                Task.deleted_at.is_(None),
                Task.user_id == user.id,
                Task.completed_at >= start,
                Task.completed_at < end,
            ).order_by(Task.completed_at).all()
        done_lines = [f"- {t.title}" for t in done_today_tasks] or ["- 无"]
        # 剩余日程：全天事件永远保留（起点为当天 00:00，按时间过滤会误删）
        remaining = [row for row in today_events
                     if row[0].all_day or row[1] >= utcnow()]
        today_event_lines = _event_lines(remaining, tz)
        open_task_lines = [f"- {t.title}" for t in open_tasks[:15]] or ["- 无"]
        tomorrow_lines = _event_lines(tomorrow_events, tz)
        summary = (
            "今日剩余日程：\n" + "\n".join(today_event_lines) + "\n"
            "今日已完成：\n" + "\n".join(done_lines) + "\n"
            "待办任务：\n" + "\n".join(open_task_lines) + "\n"
            "明日事件：\n" + "\n".join(tomorrow_lines)
        )
        title = f"🕛 午间简报 {today.isoformat()}"
        data = {"today_events": today_event_lines, "done_tasks": done_lines,
                "open_tasks": open_task_lines, "tomorrow_events": tomorrow_lines}
    else:
        done_today_tasks = []
        if _HAS_TASK:
            done_today_tasks = Task.query.filter(
                Task.status == STATUS_DONE,
                Task.deleted_at.is_(None),
                Task.user_id == user.id,
                Task.completed_at >= start,
                Task.completed_at < end,
            ).order_by(Task.completed_at).all()
        done_lines = [f"- {t.title}" for t in done_today_tasks] or ["- 无"]
        open_task_lines = [f"- {t.title}" for t in open_tasks[:15]] or ["- 无"]
        tomorrow_lines = _event_lines(tomorrow_events, tz)
        summary = (
            "今日完成任务：\n" + "\n".join(done_lines) + "\n"
            "未完成任务：\n" + "\n".join(open_task_lines) + "\n"
            "明日事件：\n" + "\n".join(tomorrow_lines)
        )
        title = f"🌙 晚间复盘 {today.isoformat()}"
        data = {"done_tasks": done_lines, "open_tasks": open_task_lines,
                "tomorrow_events": tomorrow_lines}

    # ---- 生成内容 ----
    from app.ai.llm import LLMClient, LLMError
    from app.ai.prompts import build_system_prompt, evening_prompt, morning_prompt, noon_prompt

    content = ""
    llm = LLMClient()
    if llm.is_configured:
        if kind == "morning":
            prompt = morning_prompt(summary)
        elif kind == "noon":
            prompt = noon_prompt(summary)
        else:
            prompt = evening_prompt(summary)
        try:
            content, _ = llm.chat([
                {"role": "system", "content": build_system_prompt(user)},
                {"role": "user", "content": prompt},
            ])
        except LLMError as e:
            logger.warning("简报生成失败，降级为纯文本: %s", e)
            content = _fallback_markdown(kind, data)
    else:
        content = _fallback_markdown(kind, data)

    # ---- 落库（复用当天同名简报会话）----
    conv = Conversation.query.filter(Conversation.title == title).order_by(Conversation.id.desc()).first()
    if conv is None:
        conv = Conversation(title=title)
        db.session.add(conv)
    conv.updated_at = utcnow()
    conv.messages.append(Message(role="assistant", content=content, conversation=conv))
    db.session.commit()
    return content


@register_action(
    "morning_briefing",
    description="生成今日早安简报并推送通知（参数：channel 或 channels 指定通知渠道）",
)
def morning(user, params: dict | None = None) -> None:
    """调度动作：早安简报（含对话式引导：在早报会话追加一条引导消息）。"""
    from datetime import datetime

    from app.models.conversation import Conversation, Message
    from app.services.notify_service import notify

    params = params or {}
    content = build_briefing("morning", user)

    # 对话式引导：在早报会话追加一条助手消息，用户登录后可直接续聊
    tz = user_tz(user)
    title = f"☀️ 早安简报 {datetime.now(tz).date().isoformat()}"
    conv = Conversation.query.filter(Conversation.title == title,
                                     Conversation.user_id == user.id).order_by(
        Conversation.id.desc()).first()
    guide = ("早上好！今日安排已整理好（见上方简报）。"
             "需要我帮你排优先级、调整日程或补充提醒吗？")
    if conv is not None:
        conv.updated_at = utcnow()
        conv.messages.append(Message(role="assistant", content=guide, conversation=conv))
        db.session.commit()

    from app.services.notify_service import notify_for

    notify_for("briefing", "☀️ 早安简报", content + "\n\n💬 " + guide,
               user_id=user.id, explicit=params.get("channels") or params.get("channel"))


@register_action(
    "noon_briefing",
    description="生成今日午间简报并推送通知（参数：channel 或 channels 指定通知渠道）",
)
def noon(user, params: dict | None = None) -> None:
    """调度动作：午间简报。"""
    from app.services.notify_service import notify_for

    params = params or {}
    content = build_briefing("noon", user)
    notify_for("briefing", "🕛 午间简报", content,
               user_id=user.id, explicit=params.get("channels") or params.get("channel"))


@register_action(
    "evening_review",
    description="生成今日晚间复盘并推送通知（参数：channel 或 channels 指定通知渠道）",
)
def evening(user, params: dict | None = None) -> None:
    """调度动作：晚间复盘。"""
    from app.services.notify_service import notify_for

    params = params or {}
    content = build_briefing("evening", user)
    notify_for("briefing", "🌙 晚间复盘", content,
               user_id=user.id, explicit=params.get("channels") or params.get("channel"))
