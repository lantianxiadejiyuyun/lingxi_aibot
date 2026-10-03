"""Account-scoped WebSocket transport for external clients.

Clients authenticate in the first JSON frame, never in a URL. Each connection
handles commands sequentially; reconnecting does not replay a previous message.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import json
from threading import Lock

from flask import current_app, request
from flask_sock import Sock
from simple_websocket import ConnectionClosed

from app.extensions import db
from app.utils.api_auth import resolve_token_user
from app.utils.integration_api import ApiError

AUTH_TIMEOUT = 10
TOKEN_CHECK_INTERVAL = 25
MAX_MESSAGE_BYTES = 512 * 1024
MAX_CONNECTIONS = 16
MAX_USER_CONNECTIONS = 3


class _ConnectionSlots:
    """Bound accepted sockets as well as authenticated sockets per account."""

    def __init__(self):
        self.lock = Lock()
        self.total = 0
        self.users = Counter()

    def acquire(self):
        with self.lock:
            if self.total >= MAX_CONNECTIONS:
                return False
            self.total += 1
            return True

    def authenticate(self, user_id):
        with self.lock:
            if self.users[user_id] >= MAX_USER_CONNECTIONS:
                return False
            self.users[user_id] += 1
            return True

    def release(self, user_id=None):
        with self.lock:
            self.total -= 1
            if user_id is not None:
                self.users[user_id] -= 1
                if not self.users[user_id]:
                    del self.users[user_id]


def _send(ws, payload):
    ws.send(json.dumps(payload, ensure_ascii=False, allow_nan=False))


def _error(ws, code, message, request_id=None, conversation_id=None):
    _send(ws, {
        "type": "error", "request_id": request_id,
        "conversation_id": conversation_id, "seq": 0,
        "data": {"code": code, "error": message},
    })


def _reject(ws, code, message, close_code=1008):
    try:
        _error(ws, code, message)
    finally:
        ws.close(reason=close_code, message=message)


def _parse_frame(raw):
    if not isinstance(raw, str):
        raise ValueError("请发送 JSON 文本消息")
    if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("消息不能超过 512 KiB")

    def reject_constant(_value):
        raise ValueError("JSON 数值无效")

    try:
        frame = json.loads(raw, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        raise ValueError("JSON 消息格式不正确") from None
    if not isinstance(frame, dict):
        raise ValueError("JSON 消息必须为对象")
    try:
        json.dumps(frame, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("JSON 内容无效或嵌套过深") from None
    return frame


def _identity(token):
    """Use a new ORM session each time so revoked tokens cannot stay cached."""
    db.session.remove()
    try:
        user = resolve_token_user(token)
        return (int(user.id), user.username) if user is not None else None
    finally:
        db.session.remove()


def _request_id(frame):
    value = frame.get("request_id")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 128 \
            or any(ord(character) < 32 for character in value):
        raise ValueError("request_id 必须是 1–128 字符的字符串")
    return value


def _handle_chat(ws, frame, user_id, request_id):
    from app.services.integration_chat_service import (
        create_conversation, get_conversation, iter_chat_events,
        validate_chat_payload,
    )

    payload = validate_chat_payload(frame)
    conversation_id = payload["conversation_id"]
    if conversation_id is None:
        conversation_id = create_conversation(user_id).id
    else:
        get_conversation(user_id, conversation_id)
    # Explicitly close on transport failure so the generator's account scope
    # and conversation lock are released before this server thread is reused.
    with closing(iter_chat_events(
        conversation_id, user_id, payload["message"], request_id=request_id,
    )) as events:
        for event in events:
            _send(ws, event)


def serve_connection(ws, slots):
    """Handle one accepted connection; exposed separately for isolated tests."""
    if not slots.acquire():
        _reject(ws, "connection_limit", "连接数已达上限，请稍后重试", 1013)
        return
    slot_user_id = None
    try:
        # Query credentials are easily captured in access logs and are never
        # accepted, even if the caller would also send an authentication frame.
        if request.query_string:
            _reject(ws, "invalid_auth", "请通过首帧认证，不要在 URL 中传递凭据")
            return
        raw = ws.receive(timeout=AUTH_TIMEOUT)
        if raw is None:
            _reject(ws, "auth_timeout", "请在 10 秒内发送认证消息")
            return
        try:
            first = _parse_frame(raw)
        except ValueError:
            _reject(ws, "invalid_auth", "首帧必须为 auth 认证消息")
            return
        token = first.get("token")
        if first.get("type") != "auth" or not isinstance(token, str) \
                or not token or len(token) > 512:
            _reject(ws, "invalid_auth", "首帧必须为 auth 认证消息")
            return
        identity = _identity(token)
        if identity is None:
            _reject(ws, "unauthorized", "API Token 不正确或已撤销")
            return
        user_id, username = identity
        if not slots.authenticate(user_id):
            _reject(ws, "connection_limit", "此账号同时连接数已达上限", 1013)
            return
        slot_user_id = user_id
        _send(ws, {"type": "ready", "data": {
            "user_id": user_id, "username": username, "protocol": "lingxi.v1",
        }})

        while True:
            raw = ws.receive(timeout=TOKEN_CHECK_INTERVAL)
            identity = _identity(token)
            if identity is None or identity[0] != user_id:
                _reject(ws, "unauthorized", "API Token 已撤销，请重新绑定账号")
                return
            if raw is None:
                continue
            request_id = None
            try:
                frame = _parse_frame(raw)
                request_id = _request_id(frame)
                if frame.get("type") == "ping":
                    _send(ws, {"type": "pong", "request_id": request_id, "data": {}})
                elif frame.get("type") == "chat.send":
                    if request_id is None:
                        raise ValueError("chat.send 必须提供 request_id")
                    _handle_chat(ws, frame, user_id, request_id)
                else:
                    raise ValueError("不支持此消息类型；可使用 ping 或 chat.send")
            except ApiError as exc:
                _error(ws, exc.code, str(exc), request_id)
            except ValueError as exc:
                _error(ws, "invalid_request", str(exc), request_id)
            finally:
                # Do not keep database connections checked out while a client
                # is idle; subsequent commands reload their user/conversation.
                db.session.remove()
    except ConnectionClosed:
        pass
    except Exception:
        current_app.logger.exception("客户端 WebSocket 处理失败")
        try:
            _reject(ws, "internal_error", "服务暂时不可用，请稍后重试", 1011)
        except (ConnectionClosed, OSError):
            pass
    finally:
        db.session.remove()
        slots.release(slot_user_id)


def init_integration_ws(app):
    if "integration_ws" in app.extensions:
        return
    options = dict(app.config.get("SOCK_SERVER_OPTIONS") or {})
    options["ping_interval"] = 25
    options["max_message_size"] = MAX_MESSAGE_BYTES
    app.config["SOCK_SERVER_OPTIONS"] = options
    slots = _ConnectionSlots()
    sock = Sock(app)
    app.extensions["integration_ws"] = {"sock": sock, "slots": slots}

    @sock.route("/api/v1/ws")
    def integration_websocket(ws):
        serve_connection(ws, slots)
