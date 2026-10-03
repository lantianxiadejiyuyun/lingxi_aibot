"""Account-scoped chat execution shared by the REST and WebSocket APIs."""
from __future__ import annotations

import logging
import uuid

from app.ai.executor import run_chat
from app.extensions import db
from app.models.conversation import Conversation
from app.models.user import User
from app.services.context_service import conversation_lock
from app.utils.integration_api import ApiError, api_user_context

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 100_000
MAX_CONVERSATION_ID = 2**31 - 1
CHAT_FAILURE = "对话处理失败；此前已执行的操作可能已生效，请先检查会话记录再重试。"
_MISSING_CONFIG = "未配置 API Key。请在「设置 → 模型与人设」填写自己的 Key；仍可用 /help 查看命令。"


def get_conversation(user_id: int, conversation_id: int) -> Conversation:
    _validate_conversation_id(conversation_id)
    conversation = (Conversation.query.filter_by(id=conversation_id, user_id=user_id)
                    .populate_existing().first())
    if conversation is None:
        raise ApiError("会话不存在", status=404, code="not_found")
    return conversation


def validate_title(title) -> str:
    if not isinstance(title, str) or not title.strip() or len(title) > 255:
        raise ApiError("title 必须为 1–255 个字符的文本")
    _validate_unicode(title, "title")
    return title.strip()


def _validate_unicode(value: str, field: str):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ApiError(f"{field} 包含无效的 Unicode 字符") from None


def _validate_conversation_id(value):
    if type(value) is not int or not 1 <= value <= MAX_CONVERSATION_ID:
        raise ApiError("conversation_id 必须为 1–2147483647 的整数")


def create_conversation(user_id: int, title: str = "新对话") -> Conversation:
    conversation = Conversation(user_id=user_id, title=validate_title(title))
    db.session.add(conversation)
    db.session.commit()
    return conversation


def validate_chat_payload(data, require_conversation: bool = False) -> dict:
    if not isinstance(data, dict):
        raise ApiError("请求体必须为 JSON 对象")
    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ApiError("message 必须为非空文本")
    if len(message) > MAX_MESSAGE_LENGTH:
        raise ApiError("message 不能超过 100000 个字符")
    _validate_unicode(message, "message")
    stream = data.get("stream", False)
    if not isinstance(stream, bool):
        raise ApiError("stream 必须为布尔值")
    conversation_id = data.get("conversation_id")
    if conversation_id is not None:
        _validate_conversation_id(conversation_id)
    if require_conversation and conversation_id is None:
        raise ApiError("缺少 conversation_id")
    request_id = data.get("request_id")
    if request_id is not None:
        if (not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128
                or any(ord(character) < 32 for character in request_id)):
            raise ApiError("request_id 必须为 1–128 个字符的文本，不能包含控制字符")
        _validate_unicode(request_id, "request_id")
    return {"message": message.strip(), "stream": stream,
            "conversation_id": conversation_id, "request_id": request_id}


def iter_chat_events(conversation_id: int, user_id: int, message: str,
                     request_id: str | None = None):
    """Yield one start and one terminal done, restoring account scope on close.

    An active Flask app/request context is required (also supplied by Flask-Sock).
    The request ID correlates events only; it is not an idempotency key.
    """
    normalized = validate_chat_payload({"message": message, "conversation_id": conversation_id,
                                        "request_id": request_id}, require_conversation=True)
    request_id = normalized["request_id"] or uuid.uuid4().hex
    sequence = 0

    def event(kind, payload):
        nonlocal sequence
        sequence += 1
        return {"type": kind, "conversation_id": conversation_id,
                "request_id": request_id, "seq": sequence, "data": payload}

    # Keep lookup, start, execution and the terminal event in the same lock.
    # Otherwise a queued DELETE can remove the row after start but before the
    # legacy executor takes its own (re-entrant) lock and runs command tools.
    with conversation_lock(conversation_id):
        try:
            # Authentication/prevalidation may have opened a repeatable-read
            # transaction before waiting. Route-side writes are already committed.
            db.session.rollback()
            user = db.session.get(User, user_id, populate_existing=True)
            if user is None:
                raise ApiError("账号不存在", status=401, code="unauthorized")
            conversation = get_conversation(user_id, conversation_id)
        except Exception as exc:
            if isinstance(exc, ApiError):
                error = str(exc)
            else:
                logger.error("Integration chat initialization failed for conversation %s (%s)",
                             conversation_id, type(exc).__name__)
                error = CHAT_FAILURE
            db.session.rollback()
            # SSE response headers may already be sent. A deletion while queued
            # must terminate as protocol events, never as an HTML/error traceback.
            yield event("start", {})
            yield event("error", error)
            yield event("done", "")
            return

        with api_user_context(user):
            yield event("start", {})
            iterator = None
            reply = ""
            failed = False
            completed = False
            try:
                iterator = run_chat(conversation, normalized["message"], user)
                for kind, payload in iterator:
                    if kind == "done":
                        reply = payload if isinstance(payload, str) else ""
                        completed = True
                        break
                    if kind == "error":
                        # The legacy executor emits exception text as error payloads.
                        # Only an exact, known configuration message is safe to relay.
                        failed = True
                        yield event("error", _MISSING_CONFIG if payload == _MISSING_CONFIG else CHAT_FAILURE)
                        break
                    if kind in {"ack", "delta", "tool", "notice", "title"}:
                        yield event(kind, payload)
                if not completed and not failed:
                    failed = True
                    yield event("error", CHAT_FAILURE)
            except Exception as exc:
                logger.error("Integration chat execution failed for conversation %s (%s)",
                             conversation_id, type(exc).__name__)
                db.session.rollback()
                failed = True
                yield event("error", CHAT_FAILURE)
            finally:
                if iterator is not None and hasattr(iterator, "close"):
                    iterator.close()
            yield event("done", "" if failed else reply)
