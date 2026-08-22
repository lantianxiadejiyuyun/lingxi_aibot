"""设置读写：运行时配置优先读 DB（Setting 表），缺省回退应用配置（.env）。"""
from __future__ import annotations

from typing import Any, Optional

from app.extensions import db
from app.models.setting import Setting


def get_setting(key: str, default: Any = None) -> Any:
    row = db.session.get(Setting, key)
    if row is not None and row.value is not None:
        return row.value
    return default


def set_setting(key: str, value: Any) -> None:
    row = db.session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value
    db.session.commit()


def get_setting_from(key: str, env_key: Optional[str] = None, default: Any = None) -> Any:
    """优先级：DB 设置 > 应用配置（.env）> default。"""
    from flask import current_app

    env_val = current_app.config.get(env_key or key.upper(), default)
    return get_setting(key, env_val)
