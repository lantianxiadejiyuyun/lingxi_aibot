"""Direct OneBot private chats; never interpret CQ markup as a command.

Inbound IDs are committed before AI/tools run. A failed or interrupted event is
not retried automatically: at-most-once execution is safer than repeating tools.
MySQL named locks serialize route changes with final sends across processes;
generation checks discard replies generated before a route was rebound. The
receiver queue still uses the deployment's single Gunicorn worker.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import math
import re
import secrets
import threading

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy import text as sql_text

from app.extensions import db
from app.models.conversation import Conversation
from app.models.onebot import OneBotBinding, OneBotBindingCode, OneBotEvent
from app.models.user import User
from app.services.integration_chat_service import CHAT_FAILURE, iter_chat_events
from app.utils.integration_api import ApiError, iso_datetime
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)
CODE_LIFETIME = timedelta(minutes=10)
EVENT_RETENTION = timedelta(days=7)
MAX_REPLY_LENGTH = 12000
_route_lock = threading.RLock()
_guard_state = threading.local()


@contextmanager
def _route_guard():
    """Serialize route changes/sends across worker processes without a DB txn.

    MySQL named locks belong to a dedicated connection, not a transaction. Commit
    the GET_LOCK statement before any network I/O; the notification caller's own
    transaction remains untouched. All mutations acquire this guard before rows.
    """
    if not _route_lock.acquire(timeout=15):
        raise ApiError("QQ 绑定或投递繁忙，请稍后重试", 503, "onebot_busy")
    try:
        if getattr(_guard_state, "active", False):
            yield
            return
        connection = None
        lock_name = ""
        try:
            if db.engine.dialect.name == "mysql":
                suffix = hashlib.sha256(str(db.engine.url.database).encode()).hexdigest()[:20]
                lock_name = "lingxi:onebot:" + suffix
                connection = db.engine.connect()
                acquired = connection.execute(sql_text("SELECT GET_LOCK(:name, 15)"),
                                              {"name": lock_name}).scalar()
                connection.commit()
                if acquired != 1:
                    raise ApiError("QQ 绑定或投递繁忙，请稍后重试", 503, "onebot_busy")
            _guard_state.active = True
            yield
        finally:
            _guard_state.active = False
            if connection is not None:
                try:
                    connection.execute(sql_text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})
                    connection.commit()
                except Exception:
                    # Invalidating closes the server connection and its named
                    # locks; never return a possibly locked connection to pool.
                    connection.invalidate()
                    logger.warning("OneBot route guard connection was reset")
                finally:
                    connection.close()
    finally:
        _route_lock.release()


def _account(user_id):
    if type(user_id) is not int or user_id <= 0:
        raise ApiError("账号不存在", 404, "not_found")
    user = User.query.filter_by(id=user_id).with_for_update().populate_existing().first()
    if user is None:
        raise ApiError("账号不存在", 404, "not_found")
    return user


def _hash_code(code):
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def _snapshot(row):
    if row is None:
        return None
    return {"user_id": row.user_id, "self_id": row.self_id, "qq_user_id": row.qq_user_id,
            "conversation_id": row.conversation_id, "generation": row.generation}


def _read_binding(*, user_id=None, self_id=None, qq_user_id=None):
    # A separate short transaction sees current committed data even if a caller
    # already has a MySQL repeatable-read snapshot. Never commit its transaction.
    if db.engine.dialect.name == "sqlite":
        # In-memory SQLite fixtures share one connection. Opening and rolling
        # back another Session there would roll back the caller's pending row.
        query = OneBotBinding.query.populate_existing()
        query = query.filter_by(user_id=user_id) if user_id is not None else query.filter_by(
            self_id=self_id, qq_user_id=qq_user_id)
        return _snapshot(query.first())
    with Session(db.engine) as session:
        query = session.query(OneBotBinding)
        query = query.filter_by(user_id=user_id) if user_id is not None else query.filter_by(
            self_id=self_id, qq_user_id=qq_user_id)
        return _snapshot(query.first())


def binding_view(user_id):
    binding = _read_binding(user_id=user_id)
    if binding is None:
        return {"bound": False, "self_id": "", "qq_user_id": "", "conversation_id": None}
    return {"bound": True, **{key: binding[key] for key in (
        "self_id", "qq_user_id", "conversation_id")}}


def create_binding_code(user_id):
    """Replace any previous challenge; only the SHA-256 digest is persisted."""
    code = secrets.token_hex(12).upper()
    now = utcnow()
    with _route_guard():
        try:
            _account(user_id)
            row = OneBotBindingCode.query.filter_by(user_id=user_id).with_for_update().populate_existing().first()
            if row is None:
                row = OneBotBindingCode(user_id=user_id)
                db.session.add(row)
            row.code_hash, row.expires_at, row.created_at = _hash_code(code), now + CODE_LIFETIME, now
            expires_at = row.expires_at
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
    return {"code": code, "expires_at": iso_datetime(expires_at)}


def _consume_code(self_id, qq_user_id, value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9A-Fa-f]{24}", value):
        raise ApiError("绑定码无效或已过期，请在灵犀网页重新生成")
    code_hash = _hash_code(value.upper())
    with _route_guard():
        try:
            # Discover the owner, then take locks in account -> challenge order.
            # The second locking read is essential under MySQL REPEATABLE READ.
            hint = OneBotBindingCode.query.filter_by(code_hash=code_hash).first()
            if hint is None:
                raise ApiError("绑定码无效或已过期，请在灵犀网页重新生成")
            user_id = hint.user_id
            _account(user_id)
            code = OneBotBindingCode.query.filter_by(user_id=user_id).with_for_update().populate_existing().first()
            if code is None or code.code_hash != code_hash or code.expires_at <= utcnow():
                raise ApiError("绑定码无效或已过期，请在灵犀网页重新生成")
            owner = OneBotBinding.query.filter_by(self_id=self_id, qq_user_id=qq_user_id).with_for_update().populate_existing().first()
            if owner is not None and owner.user_id != user_id:
                raise ApiError("这个 QQ 私聊已绑定其他灵犀账号，请先解绑", 409, "binding_conflict")
            row = OneBotBinding.query.filter_by(user_id=user_id).with_for_update().populate_existing().first()
            same_route = row is not None and (row.self_id, row.qq_user_id) == (self_id, qq_user_id)
            conversation = (Conversation.query.filter_by(id=row.conversation_id, user_id=user_id)
                            .with_for_update().populate_existing().first()) if same_route else None
            if conversation is None:
                conversation = Conversation(user_id=user_id, title="QQ 私聊")
                db.session.add(conversation)
                db.session.flush()
            if row is None:
                row = OneBotBinding(user_id=user_id)
                db.session.add(row)
            row.self_id, row.qq_user_id = self_id, qq_user_id
            row.conversation_id, row.generation = conversation.id, secrets.token_hex(16)
            row.updated_at = utcnow()
            db.session.delete(code)
            db.session.flush()
            result = _snapshot(row)
            db.session.commit()
            return result
        except IntegrityError:
            db.session.rollback()
            raise ApiError("绑定已变更，请刷新绑定状态后重试", 409, "binding_conflict") from None
        except Exception:
            db.session.rollback()
            raise


def unbind(user_id, *, expected_generation=None):
    with _route_guard():
        try:
            _account(user_id)
            row = OneBotBinding.query.filter_by(user_id=user_id).with_for_update().populate_existing().first()
            if expected_generation is not None and (row is None or row.generation != expected_generation):
                raise ApiError("绑定已变更，请重新检查当前私聊", 409, "binding_changed")
            if row is not None:
                db.session.delete(row)
            OneBotBindingCode.query.filter_by(user_id=user_id).delete(synchronize_session=False)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
    return binding_view(user_id)


def _send(self_id, qq_user_id, text, image_bytes=None):
    from app.services.onebot_transport import send_private
    if len(text) > MAX_REPLY_LENGTH:
        text = text[:MAX_REPLY_LENGTH - 40] + "\n\n回复较长，完整内容请在灵犀网页会话中查看。"
    send_private(self_id, qq_user_id, text, image_bytes=image_bytes)


def send_to_binding(binding, text, image_bytes=None):
    """Synchronous delivery; leave the caller's notification transaction intact."""
    with _route_guard():
        if not binding or _read_binding(user_id=binding["user_id"]) != binding:
            raise ValueError("QQ 绑定已变更，本条旧消息已停止发送")
        _send(binding["self_id"], binding["qq_user_id"], text, image_bytes=image_bytes)


def send_notification(user_id, title, body, image_bytes=None):
    if not user_id:
        raise ValueError("QQ 通知必须明确所属账号")
    binding = _read_binding(user_id=user_id)
    if binding is None:
        raise ValueError("请先绑定自己的 QQ 私聊")
    text = "\n\n".join(str(part).strip() for part in (title, body) if part is not None and str(part).strip())
    if not text and not image_bytes:
        raise ValueError("QQ 消息不能为空")
    send_to_binding(binding, text, image_bytes=image_bytes)


def _number(value, *, signed=False):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    value = str(value)
    if not re.fullmatch(r"-?[0-9]{1,20}" if signed else r"[0-9]{1,20}", value):
        return None
    number = int(value)
    return str(number) if signed or number > 0 else None


def _text(message):
    if isinstance(message, list):
        if not message or len(message) > 1000:
            return None
        pieces = []
        for segment in message:
            if not isinstance(segment, dict) or segment.get("type") != "text":
                return None
            data = segment.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("text"), str):
                return None
            pieces.append(data["text"])
        message = "".join(pieces)
    if not isinstance(message, str) or re.search(r"\[CQ:", message, re.IGNORECASE):
        return None
    try:
        message.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return message.strip()


def _claim(self_id, message_id):
    try:
        db.session.add(OneBotEvent(self_id=self_id, message_id=message_id))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return False
    # Bounded cleanup work per accepted event. Events older than this retention
    # window are rejected before claiming, so purging cannot rerun old tools.
    stale = OneBotEvent.query.filter(OneBotEvent.created_at < utcnow() - EVENT_RETENTION).limit(100).all()
    for row in stale:
        db.session.delete(row)
    db.session.commit()
    return True


def _event_time_valid(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return False
    try:
        happened = datetime.fromtimestamp(value, timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return False
    now = utcnow()
    return now - EVENT_RETENTION <= happened <= now + timedelta(minutes=5)


def _process(self_id, qq_user_id, message_id, message):
    binding = _read_binding(self_id=self_id, qq_user_id=qq_user_id)
    text = _text(message)
    if text is None:
        if binding:
            send_to_binding(binding, "QQ 直连目前支持私聊文字；图片、语音和文件请先转成文字，或使用灵犀网页。")
        return
    if not text:
        return
    if text == "/lingxi 身份":
        _send(self_id, qq_user_id, f"机器人 QQ：{self_id}\n你的 QQ：{qq_user_id}\n请在灵犀设置生成绑定码，再发送 /lingxi 绑定 <绑定码>。")
        return
    if text.startswith("/lingxi 绑定"):
        parts = text.split()
        if len(parts) != 3 or parts[:2] != ["/lingxi", "绑定"]:
            _send(self_id, qq_user_id, "用法：/lingxi 绑定 <绑定码>。绑定码在灵犀设置生成，10 分钟内有效。")
            return
        try:
            current = _consume_code(self_id, qq_user_id, parts[2])
        except ApiError as exc:
            _send(self_id, qq_user_id, str(exc))
            return
        send_to_binding(current, "QQ 私聊已绑定。现在可以直接聊天，或用 /help 查看命令；主动陪伴需要在灵犀网页单独开启。")
        return
    if text == "/lingxi 状态":
        if binding:
            send_to_binding(binding, "当前 QQ 私聊已绑定灵犀，可以直接发送文字。")
        else:
            _send(self_id, qq_user_id, "当前 QQ 私聊尚未绑定。请在灵犀设置生成绑定码。")
        return
    if text == "/lingxi 解绑":
        if binding:
            with _route_guard():
                unbind(binding["user_id"], expected_generation=binding["generation"])
                _send(self_id, qq_user_id, "QQ 私聊已解绑，原会话记录保留在灵犀网页中。")
        else:
            _send(self_id, qq_user_id, "当前 QQ 私聊尚未绑定。")
        return
    if text.startswith("/lingxi"):
        _send(self_id, qq_user_id, "可用命令：/lingxi 身份、/lingxi 绑定 <绑定码>、/lingxi 状态、/lingxi 解绑。")
        return
    if binding is None:
        return
    if len(text) > 100000:
        send_to_binding(binding, "消息过长，请缩短到 100000 个字符以内。")
        return
    # No database transaction or route lock spans AI/tool execution. A route may
    # change meanwhile; the final generation comparison suppresses its old reply.
    db.session.rollback()
    reply, error = "", ""
    events = iter_chat_events(binding["conversation_id"], binding["user_id"], text,
                             request_id=f"onebot:{self_id}:{message_id}")
    try:
        for event in events:
            if event.get("type") == "error":
                error = event.get("data") or CHAT_FAILURE
            elif event.get("type") == "done":
                reply = event.get("data") or ""
    except Exception:
        error = CHAT_FAILURE
    finally:
        if hasattr(events, "close"):
            events.close()
    db.session.rollback()
    if reply or error:
        send_to_binding(binding, str(error or reply))


def handle_event(event):
    """Process an authenticated, self_id-verified OneBot v11 event in app context."""
    if (not isinstance(event, dict) or event.get("post_type") != "message"
            or event.get("message_type") != "private" or event.get("group_id") is not None):
        return
    self_id, qq_user_id = _number(event.get("self_id")), _number(event.get("user_id"))
    message_id = _number(event.get("message_id"), signed=True)
    if not self_id or not qq_user_id or self_id == qq_user_id or message_id is None:
        return
    if not _event_time_valid(event.get("time")):
        return
    if not _claim(self_id, message_id):
        return
    status = "done"
    try:
        _process(self_id, qq_user_id, message_id, event.get("message"))
    except Exception as exc:
        status = "failed"
        db.session.rollback()
        # Exceptions and message bodies can contain credentials or private data.
        logger.warning("OneBot event failed self=%s message=%s type=%s", self_id, message_id, type(exc).__name__)
    finally:
        db.session.rollback()
        OneBotEvent.query.filter_by(self_id=self_id, message_id=message_id).update({"status": status})
        db.session.commit()
