"""Resumable download events on a separate socket, never blocked by chat tokens."""
import time

from flask import request
from flask_sock import Sock
from simple_websocket import ConnectionClosed

from app.extensions import db
from app.services.integration_ws import _ConnectionSlots, _parse_frame, _identity, _send, _reject
from app.services import media_service
from app.services.media_config import MediaError, parse_id


def serve_media_socket(ws, slots):
    if not slots.acquire():
        _reject(ws, "connection_limit", "连接数已达上限", 1013)
        return
    uid = None
    try:
        if request.query_string:
            _reject(ws, "invalid_auth", "请在首帧提供令牌，URL 不接受凭据")
            return
        try:
            first = _parse_frame(ws.receive(timeout=10))
        except (TypeError, ValueError):
            _reject(ws, "invalid_auth", "首帧必须为 auth 认证消息")
            return
        token = first.get("token", "")
        identity = _identity(token) if first.get("type") == "auth" and isinstance(token, str) else None
        if not identity:
            _reject(ws, "invalid_auth", "认证失败")
            return
        user_id = identity[0]
        if not slots.authenticate(user_id):
            _reject(ws, "connection_limit", "此账号连接数已达上限", 1013)
            return
        uid = user_id
        after, task_id, subscribed = 0, None, False
        checked = time.monotonic()
        _send(ws, {"type": "ready", "data": {"user_id": uid}})
        while True:
            if time.monotonic() - checked >= 25:
                if _identity(token) != identity:
                    _reject(ws, "invalid_auth", "令牌已失效")
                    return
                checked = time.monotonic()
            raw = ws.receive(timeout=1)
            if raw is not None:
                if _identity(token) != identity:
                    _reject(ws, "invalid_auth", "令牌已失效")
                    return
                checked = time.monotonic()
                try:
                    frame = _parse_frame(raw)
                    if frame.get("type") == "subscribe":
                        next_after = parse_id(frame.get("after", 0), minimum=0)
                        next_task = frame.get("task_id")
                        if next_task is not None:
                            next_task = parse_id(next_task)
                            media_service.get_task(uid, next_task)
                        after, task_id = next_after, next_task
                        subscribed = True
                    elif frame.get("type") == "download.action":
                        task = media_service.action(uid, parse_id(frame.get("task_id", 0)), frame.get("action"))
                        _send(ws, {"type": "ack", "data": media_service.task_dict(task)})
                    elif frame.get("type") == "ping":
                        _send(ws, {"type": "pong"})
                    else:
                        raise ValueError("未知消息类型")
                except (ValueError, TypeError, MediaError):
                    _send(ws, {"type": "error", "data": {"code": "invalid_request", "error": "消息无效或任务不可访问"}})
            if subscribed:
                db.session.remove()
                for event in media_service.events(uid, task_id, after):
                    _send(ws, event)
                    after = event["seq"]
    except (ConnectionClosed, ValueError, TypeError):
        return
    finally:
        db.session.remove()
        slots.release(uid)


def init_media_ws(app):
    sock = Sock(app)
    slots = app.extensions["integration_ws"]["slots"]

    @sock.route("/api/v1/download-events/ws")
    def media_socket(ws):
        serve_media_socket(ws, slots)
