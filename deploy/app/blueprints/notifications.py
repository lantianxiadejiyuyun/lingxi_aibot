"""通知中心：列表筛选 / 标已读 / 删除 / 全部标已读 / 清除已读（PRG）。

所有写操作均为普通 POST 表单（带 csrf_token），成功后 redirect 回当前筛选条件。
"""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models.notification import STATUS_SENT, Notification
from app.utils.timeutil import fmt_dt, user_tz

bp = Blueprint("notifications", __name__, url_prefix="/notifications")

# 渠道 / 状态 → 中文展示名（未知值原样显示）
_CHANNELS = {"inapp": "站内", "serverchan": "Server酱", "feishu": "飞书", "feishu_app": "飞书机器人(应用)"}
_STATUSES = {"pending": "待发送", "sent": "已发送", "failed": "失败"}


def _redirect_back():
    """写操作后回到当前筛选条件（channel/status）的列表页。"""
    return redirect(url_for(
        "notifications.index",
        channel=request.args.get("channel") or None,
        status=request.args.get("status") or None,
    ))


@bp.route("/")
@login_required
def index():
    channel = (request.args.get("channel") or "").strip()
    status = (request.args.get("status") or "").strip()

    q = Notification.query
    if channel in _CHANNELS:
        q = q.filter(Notification.channel == channel)
    if status in _STATUSES:
        q = q.filter(Notification.status == status)
    rows = q.order_by(Notification.created_at.desc()).limit(200).all()

    tz = user_tz(current_user)
    items = [
        {
            "id": n.id,
            "channel": n.channel,
            "channel_label": _CHANNELS.get(n.channel, n.channel),
            "title": n.title,
            "body": n.body,
            "status": n.status,
            "status_label": _STATUSES.get(n.status, n.status),
            "error": n.error,
            "read": n.read,
            "created_at": fmt_dt(n.created_at, tz),
        }
        for n in rows
    ]
    return render_template("notifications/index.html",
                           items=items, channel=channel, status=status)


@bp.route("/read/<int:notification_id>", methods=["POST"])
@login_required
def read(notification_id):
    rec = db.session.get(Notification, notification_id)
    if rec is not None and not rec.read:
        rec.read = True
        db.session.commit()
    return _redirect_back()


@bp.route("/delete/<int:notification_id>", methods=["POST"])
@login_required
def delete(notification_id):
    rec = db.session.get(Notification, notification_id)
    if rec is not None:
        db.session.delete(rec)
        db.session.commit()
    return _redirect_back()


@bp.route("/mark-all-read", methods=["POST"])
@login_required
def mark_all_read():
    Notification.query.filter_by(read=False).update({"read": True})
    db.session.commit()
    flash("已将全部通知标为已读", "success")
    return _redirect_back()


@bp.route("/clear-read", methods=["POST"])
@login_required
def clear_read():
    """清除已读通知（未读保留）。"""
    Notification.query.filter_by(read=True).delete()
    db.session.commit()
    flash("已清除已读通知记录", "success")
    return _redirect_back()
