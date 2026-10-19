"""Private-session binding and leased outbox. No AstrBot admin credentials needed.

All mutations serialize on the account row. Network I/O happens in the plugin,
outside database transactions; delivery is at least once, never promised exactly once.
"""
from datetime import timedelta
import re
import secrets

from sqlalchemy import or_

from app.extensions import db
from app.models.astrbot import AstrBotBinding, AstrBotOutbox
from app.models.conversation import Conversation, Message
from app.models.notification import Notification, STATUS_FAILED, STATUS_PENDING, STATUS_SENT
from app.models.user import User
from app.utils.integration_api import ApiError, iso_datetime
from app.utils.timeutil import utcnow

LEASE_SECONDS = 300
MAX_ATTEMPTS = 5
MAX_TEXT = 12000


def _lock_account(user_id):
    user = db.session.query(User).filter_by(id=user_id).with_for_update().populate_existing().first()
    if user is None:
        raise ApiError("账号不存在", 404, "not_found")
    return user


def _binding_locked(user_id):
    # Locking reads see committed rows even under MySQL REPEATABLE READ. A token
    # lookup may already have established an older snapshot before the account lock.
    return AstrBotBinding.query.filter_by(user_id=user_id).with_for_update().populate_existing().first()


def _session(value):
    if (not isinstance(value, str) or not value or len(value) > 512
            or value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ApiError("session 必须是有效的 AstrBot 私聊身份，最长 512 字符")
    parts = value.split(":", 2)
    if len(parts) != 3 or not parts[0] or parts[1] != "FriendMessage" or not parts[2]:
        raise ApiError("仅支持 platform_id:FriendMessage:session_id 格式的私聊")
    return value


def binding_view(user_id):
    row = db.session.get(AstrBotBinding, user_id)
    if row is None:
        return {"bound": False, "enabled": False, "session": "", "conversation_id": None}
    conversation = Conversation.query.filter_by(id=row.conversation_id, user_id=user_id).first()
    return {"bound": True, "enabled": True, "session": row.session,
            "conversation_id": conversation.id if conversation else None,
            "updated_at": iso_datetime(row.updated_at)}


def _notification(row, status, error=""):
    if row.notification_id is None:
        return
    rec = Notification.query.filter_by(id=row.notification_id, user_id=row.user_id, channel="astrbot").with_for_update().populate_existing().first()
    if rec:
        rec.status, rec.error = status, error
        rec.sent_at = row.sent_at if status == STATUS_SENT else None


def _cancel(user_id):
    for row in AstrBotOutbox.query.filter_by(user_id=user_id, status="pending").with_for_update().populate_existing().all():
        row.status = "cancelled"
        row.lease_token, row.lease_until = "", None
        _notification(row, STATUS_FAILED, "AstrBot 已解绑或变更接收私聊")


def bind(user_id, session):
    session = _session(session)
    _lock_account(user_id)
    row = _binding_locked(user_id)
    changed = row is not None and row.session != session
    conversation = (Conversation.query.filter_by(id=row.conversation_id, user_id=user_id).with_for_update().populate_existing().first()
                    if row and not changed else None)
    if changed:
        _cancel(user_id)
    if conversation is None:
        conversation = Conversation(user_id=user_id, title="AstrBot 私聊")
        db.session.add(conversation)
        db.session.flush()
    if row is None:
        row = AstrBotBinding(user_id=user_id, session=session, conversation_id=conversation.id)
        db.session.add(row)
    else:
        row.session, row.conversation_id, row.updated_at = session, conversation.id, utcnow()
    db.session.commit()
    return binding_view(user_id)


def unbind(user_id, expected_session=None):
    _lock_account(user_id)
    row = _binding_locked(user_id)
    if expected_session is not None and row and row.session != _session(expected_session):
        raise ApiError("绑定已变更，请核对当前私聊", 409, "binding_changed")
    _cancel(user_id)
    if row:
        db.session.delete(row)
    db.session.commit()
    return binding_view(user_id)


def enqueue(user_id, title, body, notification_id=None, kind="notification"):
    """Queue within notify()'s transaction; the caller commits all records together."""
    _lock_account(user_id)
    binding = _binding_locked(user_id)
    if binding is None:
        raise ValueError("请先在 AstrBot 插件中绑定自己的私聊")
    text = "\n\n".join(str(part).strip() for part in (title, body) if str(part).strip())
    if not text or len(text) > MAX_TEXT:
        raise ValueError(f"AstrBot 消息须为 1–{MAX_TEXT} 字符；请缩短正文")
    now = utcnow()
    row = AstrBotOutbox(user_id=user_id, notification_id=notification_id, session=binding.session, kind=kind,
                        conversation_id=binding.conversation_id, text=text,
                        expires_at=now + timedelta(hours=24), available_at=now)
    db.session.add(row)
    db.session.flush()
    return row


def claim(user_id, limit=1, *, now=None):
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ApiError("limit 必须为 1–10 的整数")
    now = now or utcnow()
    user = _lock_account(user_id)
    binding = _binding_locked(user_id)
    if binding is None:
        db.session.commit()
        return []
    # Failed/expired deliveries remain visible; no old message suddenly resurfaces.
    expired = AstrBotOutbox.query.filter_by(user_id=user_id, status="pending").filter(
        or_(AstrBotOutbox.expires_at <= now,
            (AstrBotOutbox.attempts >= MAX_ATTEMPTS) & (AstrBotOutbox.lease_until <= now)))
    for row in expired.with_for_update().populate_existing().all():
        row.status, row.lease_until = "failed", None
        _notification(row, STATUS_FAILED, "AstrBot 投递已过期或达到重试上限")
    query = AstrBotOutbox.query.filter_by(user_id=user_id, session=binding.session, status="pending").filter(
        AstrBotOutbox.available_at <= now, AstrBotOutbox.expires_at > now,
        AstrBotOutbox.attempts < MAX_ATTEMPTS,
        or_(AstrBotOutbox.lease_until.is_(None), AstrBotOutbox.lease_until <= now),
    )
    from app.services.companion_service import delivery_block_reason
    block_reason = delivery_block_reason(user, now=now)
    if block_reason:
        if block_reason == "disabled":
            for row in query.filter_by(kind="companion").with_for_update().populate_existing().all():
                row.status, row.lease_token, row.lease_until = "cancelled", "", None
                _notification(row, STATUS_FAILED, "主动陪伴已关闭，此消息已取消")
        query = query.filter(AstrBotOutbox.kind != "companion")
    rows = query.order_by(AstrBotOutbox.id).limit(limit).with_for_update().populate_existing().all()
    result = []
    for row in rows:
        row.lease_token = secrets.token_hex(24)
        row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        row.attempts += 1
        result.append({"id": row.id, "session": row.session, "text": row.text,
                       "lease_token": row.lease_token, "lease_until": iso_datetime(row.lease_until),
                       "conversation_id": row.conversation_id})
    db.session.commit()
    return result


def acknowledge(user_id, message_id, lease_token, outcome):
    if (type(message_id) is not int or not 1 <= message_id <= 2147483647
            or not isinstance(lease_token, str) or not re.fullmatch(r"[0-9a-f]{48}", lease_token)
            or outcome not in ("sent", "failed")):
        raise ApiError("请提供 lease_token 和 outcome（sent/failed）")
    _lock_account(user_id)
    row = AstrBotOutbox.query.filter_by(id=message_id, user_id=user_id).with_for_update().populate_existing().first()
    if row is None:
        raise ApiError("消息不存在", 404, "not_found")
    if not secrets.compare_digest(row.lease_token, lease_token):
        raise ApiError("租约已失效，请重新领取", 409, "stale_lease")
    if row.status == "sent" and outcome == "sent":
        db.session.commit()
        return {"id": row.id, "status": "sent"}
    if row.status != "pending" or row.lease_until is None:
        raise ApiError("消息不在当前投递租约内", 409, "stale_lease")
    now = utcnow()
    if outcome == "sent":
        row.status, row.sent_at, row.lease_until = "sent", now, None
        _notification(row, STATUS_SENT)
        conversation = Conversation.query.filter_by(id=row.conversation_id, user_id=user_id).with_for_update().populate_existing().first()
        if conversation:
            conversation.messages.append(Message(role="assistant", content=row.text))
            conversation.updated_at = now
    else:
        row.lease_until = None
        row.lease_token = ""
        if row.attempts >= MAX_ATTEMPTS or row.expires_at <= now:
            row.status = "failed"
            _notification(row, STATUS_FAILED, "AstrBot 投递失败，已停止自动重试")
        else:
            row.available_at = now + timedelta(seconds=min(1800, 30 * 2 ** row.attempts))
            _notification(row, STATUS_PENDING, "AstrBot 暂未送达，稍后重试")
    db.session.commit()
    return {"id": row.id, "status": row.status}
