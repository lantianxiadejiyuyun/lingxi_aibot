"""设置读写：运行时配置优先读 DB（Setting 表），缺省回退应用配置（.env）。

多用户：user_id=0 为全局，user_id>0 为用户级；读取时用户级优先、缺省回退全局。
"""
from __future__ import annotations

from typing import Any, Optional

from app.extensions import db
from app.models.setting import Setting
from app.utils.scoping import current_user_id

GLOBAL_USER = 0


def get_setting(key: str, default: Any = None, user_id: Optional[int] = None) -> Any:
    """读设置：用户级优先，缺省回退全局。user_id=None 时自动解析当前用户。"""
    uid = user_id if user_id is not None else current_user_id()
    if uid:
        row = Setting.query.filter_by(key=key, user_id=uid).first()
        if row is not None and row.value is not None:
            return row.value
    row = Setting.query.filter_by(key=key, user_id=GLOBAL_USER).first()
    if row is not None and row.value is not None:
        return row.value
    return default


def get_own_setting(key: str, default: Any = None, user_id: Optional[int] = None) -> Any:
    """只读当前用户自己的设置，不回退全局、不读 .env。

    用于 API Key 等不能共享的配置。未登录 / 调度未绑定用户时返回 default。
    """
    uid = user_id if user_id is not None else current_user_id()
    if not uid:
        return default
    row = Setting.query.filter_by(key=key, user_id=int(uid)).first()
    if row is not None and row.value is not None:
        return row.value
    return default


def set_setting(key: str, value: Any, user_id: Optional[int] = None) -> None:
    """写设置到当前用户（user_id=None 自动解析；0 为全局）。"""
    uid = user_id if user_id is not None else current_user_id()
    row = Setting.query.filter_by(key=key, user_id=uid).first()
    if row is None:
        row = Setting(key=key, user_id=uid, value=value)
        db.session.add(row)
    else:
        row.value = value
    db.session.commit()


def get_setting_from(key: str, env_key: Optional[str] = None, default: Any = None,
                     user_id: Optional[int] = None) -> Any:
    """优先级：DB 设置（用户级 > 全局）> 应用配置（.env）> default。"""
    from flask import current_app

    env_val = current_app.config.get(env_key or key.upper(), default)
    return get_setting(key, env_val, user_id=user_id)
