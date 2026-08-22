"""App 对接 API 的 Token 鉴权：请求头 X-API-Token 或 Authorization: Bearer <token>。

解析顺序：
  1. users.api_token 命中 → 数据记到该用户（设置页「账号」可生成）
  2. 与 .env API_TOKEN 相同 → 回退第一个管理员（兼容旧的全局 Token）
  3. 都没有配置时 401，不会裸奔
"""
from __future__ import annotations

import hmac
import secrets
from functools import wraps

from flask import current_app, g, jsonify, request


def _provided_token() -> str:
    header = request.headers.get("X-API-Token") or ""
    if header:
        return header.strip()
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def new_api_token() -> str:
    """生成用户级 Token（lx_ 前缀，便于辨认）。"""
    return "lx_" + secrets.token_urlsafe(32)


def assign_user_api_token(user) -> str:
    """为用户生成并保存唯一 Token，返回明文（只在生成时展示一次）。"""
    from app.extensions import db
    from app.models.user import User

    for _ in range(8):
        token = new_api_token()
        if User.query.filter_by(api_token=token).first() is None:
            user.api_token = token
            db.session.commit()
            return token
    raise RuntimeError("无法生成唯一 API Token，请重试")


def revoke_user_api_token(user) -> None:
    from app.extensions import db

    user.api_token = None
    db.session.commit()


def _token_eq(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        return hmac.compare_digest(a, b)
    except (TypeError, ValueError):
        return False


def resolve_api_user():
    """根据请求 Token 解析归属用户；无法识别返回 None。"""
    provided = _provided_token()
    if not provided:
        return None
    from app.models.user import User

    user = User.query.filter_by(api_token=provided).first()
    if user is not None:
        return user
    env_token = (current_app.config.get("API_TOKEN") or "").strip()
    if _token_eq(provided, env_token):
        return (User.query.filter_by(is_admin=True).order_by(User.id).first()
                or User.query.order_by(User.id).first())
    return None


def _has_any_token() -> bool:
    env_token = (current_app.config.get("API_TOKEN") or "").strip()
    if env_token:
        return True
    from app.models.user import User
    from sqlalchemy import and_

    return User.query.filter(and_(User.api_token.isnot(None), User.api_token != "")).first() is not None


def require_api_token(fn):
    """装饰器：校验 API Token，并把归属用户挂到 flask.g.api_user。"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = resolve_api_user()
        if user is None:
            if not _provided_token():
                if not _has_any_token():
                    return jsonify({
                        "ok": False,
                        "error": "未配置 API Token：请在「设置 → 账号」生成，或在 .env 设置 API_TOKEN",
                    }), 401
                return jsonify({"ok": False, "error": "缺少 API Token"}), 401
            return jsonify({"ok": False, "error": "API Token 不正确"}), 401
        g.api_user = user
        return fn(*args, **kwargs)
    return wrapper
