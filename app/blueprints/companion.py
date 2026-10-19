"""主动陪伴设置与活动记录；浏览和保存均不触发消息发送。"""
from __future__ import annotations

from datetime import datetime
import re
from urllib.parse import urlsplit

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models.conversation import Conversation
from app.services import astrbot_service, companion_service
from app.services.notify_service import GROUP_PREFIX, available_channels, list_notify_groups
from app.utils.scoping import user_scope
from app.utils.timeutil import user_tz

bp = Blueprint("companion", __name__, url_prefix="/companion")

_STATUSES = {
    "evaluating": ("正在判断", "tag-gray"),
    "skip": ("这次先不打扰", "tag-gray"),
    "failed": ("未完成", "tag-red"),
    "dispatching": ("正在发送", "tag-gray"),
    "queued": ("等待渠道送达", "tag-gray"),
    "sent": ("已送达", "tag-green"),
    "partial": ("部分渠道未送达", "tag-red"),
    "unknown": ("送达状态未知", "tag-red"),
}
_REASONS = {
    "disabled": "主动陪伴已关闭。",
    "outside_active_hours": "还没到你设定的活动时段。",
    "quiet_hours": "现在是免打扰时间。",
    "daily_limit": "今天的主动消息已经够了。",
    "min_interval": "距离上次消息还不够久，稍后再看看。",
    "decision_cooldown": "刚刚看过近况，稍后再看看。",
    "chat_cooldown": "刚刚聊过，这次先不打扰。",
    "calendar_busy": "你现在有日程安排，等忙完再聊。",
    "awaiting_reply": "先留一点空间，等你有空再聊。",
    "nothing_to_share": "暂时没有值得分享的新内容。",
    "duplicate": "这个话题最近已经分享过。",
    "model_unconfigured": "尚未配置可用的聊天模型。",
    "model_error": "这次生成未完成，稍后会再检查。",
    "decision_limit": "今天已经看过几次近况，明天再来。",
    "lease_expired": "这次处理已结束，等待下次合适的时间。",
    "busy": "另一次活动还在处理中。",
    "sharing": "想把这件事分享给你。",
    "preparing": "正在看看有没有值得和你聊的新内容。",
    "evaluation_interrupted": "这次判断中断了，稍后再看看。",
    "delivery_unknown": "发送过程中断，无法确认是否送达；为避免重复，没有自动补发。",
    "delivery_failed": "渠道未能送达，可到通知中心查看。",
}


def _source_url(value):
    """本版只将已知 B 站视频格式变成链接，不解析或执行素材里的地址。"""
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.netloc != "www.bilibili.com":
            return ""
        if not re.fullmatch(r"/video/(?:BV[A-Za-z0-9]{10}|av[0-9]{1,20})/?", parsed.path):
            return ""
        return "https://www.bilibili.com" + parsed.path.rstrip("/") + "/"
    except ValueError:
        return ""


def _time_text(value):
    if not value:
        return ""
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(user_tz(current_user))
        return stamp.strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


def _conversation_link(value):
    if type(value) is not int or value <= 0:
        return ""
    row = db.session.get(Conversation, value)
    if row is None or row.user_id != current_user.id:
        return ""
    return url_for("chat.index", conversation=value)


@bp.get("/")
@login_required
def index():
    with user_scope(current_user.id):
        view = companion_service.get_view(current_user, limit=20)
        binding = astrbot_service.binding_view(current_user.id)
        channels = available_channels()
        channels.extend({"name": GROUP_PREFIX + name, "display_name": "通知组：" + name,
                         "configured": True} for name in list_notify_groups())
    activities = []
    for raw in view.get("activities", []):
        label, badge = _STATUSES.get(raw.get("status"), ("已记录", "tag-gray"))
        sources = [{"title": source.get("title") or "来源资料",
                    "url": _source_url(source.get("url"))}
                   for source in raw.get("sources", []) if isinstance(source, dict)]
        activities.append({
            "status_label": label, "badge": badge,
            "reason_text": _REASONS.get(raw.get("reason"), ""),
            "show_delivery_note": raw.get("reason") == "delivery_unknown",
            "body": raw.get("body") or "", "sources": sources,
            "time_text": _time_text(raw.get("created_at")),
            "conversation_url": _conversation_link(raw.get("conversation_id")),
        })
    state = view.get("state") or {}
    return render_template("companion/index.html", config=view["config"],
                           activities=activities, binding=binding, channels=channels,
                           timezone=str(user_tz(current_user)),
                           last_attempt=_time_text(state.get("last_attempt_at")),
                           last_sent=_time_text(state.get("last_sent_at")))


@bp.post("/save")
@login_required
def save():
    values = {key: request.form.get(key, "") for key in (
        "active_start", "active_end", "quiet_start", "quiet_end",
        "min_interval_minutes", "daily_limit", "chat_cooldown_minutes", "interests",
    )}
    values.update(enabled=request.form.get("enabled") == "1",
                  share_bilibili=request.form.get("share_bilibili") == "1",
                  channels=request.form.getlist("channels"))
    try:
        with user_scope(current_user.id):
            companion_service.save_config(current_user, values)
        flash("已保存。灵犀会按你的节奏，在有内容时再来聊。", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("companion.index"))


@bp.post("/astrbot/unbind")
@login_required
def unbind():
    with user_scope(current_user.id):
        astrbot_service.unbind(current_user.id)
    flash("已解除 AstrBot 绑定。需要时可在原私聊里重新绑定。", "success")
    return redirect(url_for("companion.index"))
