"""NapCat OneBot v11 reverse events and confirmed HTTP private-message delivery.

The socket reader only validates and queues events. Each accepted connection owns
one worker, so an AI call never prevents heartbeats from being read. HTTP delivery
also works from scheduler/media processes which do not own the WebSocket.
"""
from __future__ import annotations

import base64
import hmac
import json
from queue import Empty, Full, Queue
import re
from threading import Event, Lock, Thread
from urllib.parse import urlsplit

from flask import current_app, request
from flask_sock import Sock
import requests
from simple_websocket import ConnectionClosed

from app.extensions import db

MAX_CONNECTIONS = 2
MAX_PENDING_EVENTS = 16
MAX_EVENT_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 128 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 32000
TEXT_CHUNK_CHARS = 2000
RECEIVE_INTERVAL = 10
HTTP_TIMEOUT = (5, 25)


class OneBotTransportError(RuntimeError):
    """Safe error without upstream bodies, credentials, or message content.

    ``partial`` and ``unknown`` must never cause an automatic replay. ``uncertain``
    additionally records an ambiguous final chunk after some confirmed deliveries.
    """

    def __init__(self, code, *, delivery_state="not_sent", message_ids=(), uncertain=False):
        self.code = code
        self.message_ids = list(message_ids)
        self.delivery_state = "partial" if self.message_ids else delivery_state
        self.uncertain = uncertain or delivery_state == "unknown"
        description = {
            "not_sent": "OneBot 发送失败",
            "partial": "QQ 已发送部分内容，请先检查 QQ，避免重复发送",
            "unknown": "QQ 发送结果未确认，请先检查 QQ，避免重复发送",
        }.get(self.delivery_state, "OneBot 发送失败")
        super().__init__(f"{description}（{code}）")


def _enabled(app):
    return str(app.config.get("ONEBOT_ENABLED", False)).lower() in ("true", "1", "yes", "on")


def _token(app):
    value = app.config.get("ONEBOT_ACCESS_TOKEN")
    if not isinstance(value, str) or not 32 <= len(value) <= 512:
        return None
    if not value.isascii() or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        return None
    return value


def _qq_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("invalid QQ ID")
    text = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,15}", text) or int(text) > 9007199254740991:
        raise ValueError("invalid QQ ID")
    return text


def _authorized(app, authorization):
    token = _token(app)
    if not _enabled(app) or token is None or not isinstance(authorization, str):
        return False
    # Bearer scheme is case-insensitive; the token itself is exact and compared
    # in constant time. No cookie or query-string authentication is supported.
    scheme, separator, supplied = authorization.partition(" ")
    return bool(separator and scheme.lower() == "bearer" and supplied.isascii()
                and hmac.compare_digest(supplied, token))


def _parse_event(raw, self_id):
    if not isinstance(raw, str):
        raise ValueError("invalid event")
    try:
        if len(raw.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("oversize event")

        def reject_constant(_value):
            raise ValueError("invalid number")

        event = json.loads(raw, parse_constant=reject_constant)
        if not isinstance(event, dict) or _qq_id(event.get("self_id")) != self_id:
            raise ValueError("invalid identity")
        # Also rejects lone surrogate escapes and overly deep JSON structures.
        json.dumps(event, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if event.get("post_type") != "message" or event.get("message_type") != "private":
            return None
        sender_id = _qq_id(event.get("user_id"))
        if sender_id == self_id:
            return None
        sender = event.get("sender")
        if sender is not None and (not isinstance(sender, dict) or (
                sender.get("user_id") is not None and _qq_id(sender["user_id"]) != sender_id)):
            raise ValueError("invalid sender")
        return event
    except (UnicodeError, RecursionError, TypeError, ValueError):
        raise ValueError("invalid OneBot event") from None


class _Registry:
    def __init__(self):
        self.lock = Lock()
        self.connections = {}

    def acquire(self, connection):
        with self.lock:
            if connection.self_id in self.connections or len(self.connections) >= MAX_CONNECTIONS:
                return False
            self.connections[connection.self_id] = connection
            return True

    def release(self, connection):
        with self.lock:
            if self.connections.get(connection.self_id) is connection:
                del self.connections[connection.self_id]

    def status(self):
        with self.lock:
            connected_ids = sorted(item.self_id for item in self.connections.values() if item.connected)
        return {"connected": bool(connected_ids), "self_id": connected_ids[0] if connected_ids else None}


def _dispatch_event(event):
    from app.services.onebot_service import handle_event
    handle_event(event)


class _Connection:
    def __init__(self, app, registry, self_id, authorization):
        self.app = app
        self.registry = registry
        self.self_id = self_id
        self.authorization = authorization
        self.queue = Queue(maxsize=MAX_PENDING_EVENTS)
        self.disconnected = Event()
        self.connected = True
        self.worker = Thread(target=self._work, name="onebot-events", daemon=True)

    def start(self):
        self.worker.start()

    def stop(self):
        # Already-started tools finish. Accepted pending events drain in order,
        # unless credentials are revoked. Keep the registry slot until drained to
        # stop a reconnect from executing the same account concurrently.
        with self.registry.lock:
            self.connected = False
            self.disconnected.set()

    def _work(self):
        try:
            while not (self.disconnected.is_set() and self.queue.empty()):
                try:
                    event = self.queue.get(timeout=0.1)
                except Empty:
                    continue
                try:
                    if _authorized(self.app, self.authorization):
                        with self.app.app_context():
                            try:
                                _dispatch_event(event)
                            finally:
                                db.session.remove()
                except Exception:
                    # Upstream exceptions can embed prompts, URLs and tokens.
                    self.app.logger.error("OneBot 私聊事件处理失败")
                finally:
                    self.queue.task_done()
        finally:
            self.registry.release(self)


def _close(ws, code=1008):
    try:
        ws.close(reason=code, message="OneBot connection rejected")
    except (ConnectionClosed, OSError):
        pass


def serve_connection(ws, registry):
    """Validate official OneBot handshake headers; never wait for an auth frame."""
    app = current_app._get_current_object()
    authorization = request.headers.get("Authorization", "")
    if request.query_string or not _authorized(app, authorization):
        _close(ws)
        return
    try:
        self_id = _qq_id(request.headers.get("X-Self-ID"))
        if request.headers.get("X-Client-Role") not in ("Universal", "Event"):
            raise ValueError("invalid role")
    except ValueError:
        _close(ws)
        return
    connection = _Connection(app, registry, self_id, authorization)
    if not registry.acquire(connection):
        _close(ws, 1013)
        return
    started = False
    try:
        connection.start()
        started = True
        while _authorized(app, authorization):
            raw = ws.receive(timeout=RECEIVE_INTERVAL)
            if not _authorized(app, authorization):
                break
            if raw is None:
                continue
            try:
                event = _parse_event(raw, self_id)
            except ValueError:
                _close(ws)
                return
            if event is not None:
                try:
                    connection.queue.put_nowait(event)
                except Full:
                    app.logger.warning("OneBot 私聊队列已满，已断开连接")
                    _close(ws, 1013)
                    return
        _close(ws)
    except (ConnectionClosed, OSError):
        pass
    except Exception:
        app.logger.error("OneBot 连接处理失败")
        _close(ws, 1011)
    finally:
        connection.stop()
        if not started:
            registry.release(connection)


def connection_status():
    app = current_app._get_current_object()
    extension = app.extensions.get("onebot_transport")
    status = extension["registry"].status() if extension else {"connected": False, "self_id": None}
    return {"enabled": _enabled(app), **status}


def init_onebot(app):
    if "onebot_transport" in app.extensions:
        return
    options = dict(app.config.get("SOCK_SERVER_OPTIONS") or {})
    options.setdefault("ping_interval", 25)
    options.setdefault("max_message_size", MAX_EVENT_BYTES)
    app.config["SOCK_SERVER_OPTIONS"] = options
    registry = _Registry()
    sock = Sock(app)
    app.extensions["onebot_transport"] = {"sock": sock, "registry": registry}

    @sock.route("/api/v1/integrations/onebot/ws")
    def onebot_websocket(ws):
        serve_connection(ws, registry)


def _api_configuration(app):
    token = _token(app)
    if not _enabled(app) or token is None:
        raise OneBotTransportError("not_configured")
    base = app.config.get("ONEBOT_API_URL", "")
    if not isinstance(base, str) or not base or len(base) > 2048 or any(ord(c) <= 32 for c in base):
        raise OneBotTransportError("invalid_api_url")
    try:
        parts = urlsplit(base)
        port = parts.port
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError("invalid API URL")
    except ValueError:
        raise OneBotTransportError("invalid_api_url") from None
    return base.rstrip("/"), token


def _api_call(session, base, action, payload, *, sending=False):
    state = "unknown" if sending else "not_sent"
    response = None
    try:
        response = session.post(base + "/" + action, json=payload,
                                timeout=HTTP_TIMEOUT, allow_redirects=False, stream=True)
        if response.status_code != 200:
            raise OneBotTransportError("http_error", delivery_state=state)
        content = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            content.extend(chunk)
            if len(content) > MAX_RESPONSE_BYTES:
                raise OneBotTransportError("invalid_response", delivery_state=state)
        try:
            result = json.loads(content)
        except (ValueError, UnicodeError, RecursionError):
            raise OneBotTransportError("invalid_response", delivery_state=state) from None
        if not isinstance(result, dict):
            raise OneBotTransportError("invalid_response", delivery_state=state)
        # Async retcode=1 is not a delivery confirmation. Only the documented
        # synchronous status/retcode pair is accepted.
        if result.get("status") != "ok" or type(result.get("retcode")) is not int or result["retcode"] != 0:
            known_failure = result.get("status") == "failed" and type(result.get("retcode")) is int
            raise OneBotTransportError("api_rejected" if known_failure else "unconfirmed_response",
                                       delivery_state="not_sent" if known_failure else state)
        if not isinstance(result.get("data"), dict):
            raise OneBotTransportError("invalid_response", delivery_state=state)
        return result["data"]
    except requests.RequestException:
        raise OneBotTransportError("network_error", delivery_state=state) from None
    finally:
        if response is not None:
            response.close()


def send_private(self_id, qq_user_id, text, image_bytes=None):
    """Send literal text/image segments after verifying the logged-in QQ account.

    This function never retries a send. Callers must preserve partial/unknown
    results rather than replaying the complete notification or AI response.
    """
    try:
        self_id, qq_user_id = _qq_id(self_id), _qq_id(qq_user_id)
        if self_id == qq_user_id:
            raise ValueError("cannot send to self")
    except ValueError:
        raise OneBotTransportError("invalid_identity") from None
    if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
        raise OneBotTransportError("invalid_message")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise OneBotTransportError("invalid_message") from None
    if image_bytes is not None and (not isinstance(image_bytes, bytes)
                                   or not 0 < len(image_bytes) <= MAX_IMAGE_BYTES):
        raise OneBotTransportError("invalid_image")
    if not text and image_bytes is None:
        raise OneBotTransportError("invalid_message")
    chunks = [text[i:i + TEXT_CHUNK_CHARS] for i in range(0, len(text), TEXT_CHUNK_CHARS)] or [""]
    base, token = _api_configuration(current_app)
    message_ids = []
    with requests.Session() as session:
        session.trust_env = False
        session.headers.update({"Authorization": "Bearer " + token, "Accept": "application/json"})
        try:
            login = _api_call(session, base, "get_login_info", {})
            try:
                if _qq_id(login.get("user_id")) != self_id:
                    raise ValueError("account changed")
            except ValueError:
                raise OneBotTransportError("account_mismatch") from None
            for index, chunk in enumerate(chunks):
                segments = [{"type": "text", "data": {"text": chunk}}] if chunk else []
                if index == len(chunks) - 1 and image_bytes is not None:
                    segments.append({"type": "image", "data": {
                        "file": "base64://" + base64.b64encode(image_bytes).decode("ascii"),
                    }})
                data = _api_call(session, base, "send_private_msg", {
                    "user_id": int(qq_user_id), "message": segments, "auto_escape": True,
                }, sending=True)
                # OneBot int32 message IDs can be negative. Bool/None/string are
                # not a documented confirmation and must never mark a send done.
                if type(data.get("message_id")) is not int:
                    raise OneBotTransportError("missing_message_id", delivery_state="unknown")
                message_ids.append(data["message_id"])
        except OneBotTransportError as exc:
            raise OneBotTransportError(exc.code, delivery_state=exc.delivery_state,
                                       message_ids=message_ids, uncertain=exc.uncertain) from None
    return {"ok": True, "message_ids": message_ids}
