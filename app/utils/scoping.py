"""当前用户上下文解析：网页请求取 Flask-Login current_user，调度线程取显式设置的 user。

用于设置等需要「当前归属用户」的读写（0 = 全局）。
"""
from __future__ import annotations

import threading

_local = threading.local()


def set_current_user_id(user_id: int) -> None:
    """调度线程执行动作前设置当前用户 id。"""
    _local.user_id = int(user_id)


def clear_current_user_id() -> None:
    _local.user_id = None


def current_user_id() -> int:
    """当前用户 id：调度线程优先，其次 Flask-Login，最后 0（全局）。"""
    uid = getattr(_local, "user_id", None)
    if uid:
        return int(uid)
    try:
        from flask_login import current_user

        if current_user.is_authenticated:
            return int(current_user.id)
    except Exception:  # noqa: BLE001 —— 无请求上下文
        pass
    return 0
