"""Shared contracts for authenticated integrations (never a browser login)."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
import json
import re

from flask import g, jsonify, request
from werkzeug.exceptions import HTTPException

from app.extensions import recover_session
from app.utils.api_auth import resolve_api_user
from app.utils.scoping import user_scope


class ApiError(ValueError):
    def __init__(self, message: str, status: int = 400, code: str = "invalid_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def success(data=None, status=200, **extra):
    return jsonify(ok=True, data=data, **extra), status


def error_response(message, status=400, code="invalid_request"):
    response = jsonify(ok=False, error=message, code=code)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    if status == 401:
        response.headers["WWW-Authenticate"] = 'Bearer realm="lingxi"'
    return response


@contextmanager
def api_user_context(user):
    """Bind legacy tools to the Token owner without writing a login cookie."""
    sentinel = object()
    previous_login = g.get("_login_user", sentinel)
    previous_api = g.get("api_user", sentinel)
    g._login_user = user
    g.api_user = user
    try:
        with user_scope(user.id):
            yield user
    finally:
        for key, previous in (("_login_user", previous_login), ("api_user", previous_api)):
            if previous is sentinel:
                g.pop(key, None)
            else:
                setattr(g, key, previous)


def api_authenticated(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user = resolve_api_user()
        if user is None:
            raise ApiError("缺少或无效的 API Token", 401, "unauthorized")
        with api_user_context(user):
            return fn(*args, **kwargs)
    return wrapped


def json_body():
    # Keep the limit local: long-context chat and settings have other budgets.
    request.max_content_length = 1024 * 1024
    try:
        value = request.get_json(silent=True)
        # JSON may contain escaped lone surrogates or non-finite numbers that
        # Python accepts but UTF-8 storage and interoperable clients cannot.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, RecursionError, UnicodeError):
        raise ApiError("JSON 内容无效或嵌套过深") from None
    if not isinstance(value, dict):
        raise ApiError("请求体必须是 JSON 对象")
    return value


def pagination():
    def number(name, default, minimum, maximum):
        raw = request.args.get(name, str(default))
        if not raw.isascii() or not raw.isdecimal():
            raise ApiError(f"{name} 必须是整数")
        value = int(raw) if len(raw) <= 10 else maximum + 1
        if not minimum <= value <= maximum:
            raise ApiError(f"{name} 应在 {minimum} 到 {maximum} 之间")
        return value
    return number("limit", 50, 1, 200), number("offset", 0, 0, 1000000)


def parse_datetime(value, field, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})", value
    ):
        raise ApiError(f"{field} 必须是带时区的 RFC 3339 时间")
    # fromisoformat normalizes offsets such as +08:99 instead of rejecting
    # them. An API deadline must never silently shift on malformed input.
    if value[-1] != "Z" and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        raise ApiError(f"{field} 的时区偏移无效")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("missing timezone")
        return result.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError):
        raise ApiError(f"{field} 必须是带时区的 RFC 3339 时间") from None


def iso_datetime(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def register_api_errors(bp):
    @bp.errorhandler(ApiError)
    def invalid_request(exc):
        recover_session()
        return error_response(str(exc), exc.status, exc.code)

    @bp.errorhandler(HTTPException)
    def http_error(exc):
        return error_response(exc.name, exc.code or 500, "http_error")

    @bp.errorhandler(Exception)
    def unexpected_error(exc):
        from flask import current_app
        recover_session()
        # Avoid provider credentials or request content in outward responses.
        current_app.logger.error("Integration API failed (%s)", type(exc).__name__)
        return error_response("服务暂时不可用，请稍后重试", 500, "internal_error")
