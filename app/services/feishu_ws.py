"""飞书官方 SDK 长连接（WebSocket）：无需公网回调地址，进程主动连开放平台收事件。

后台「飞书机器人 → 接入方式」选「官方 SDK 长连接」后启动。
飞书开放平台事件订阅需选择「使用长连接接收事件」。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional

from app.services.settings_service import get_setting, get_setting_from

logger = logging.getLogger(__name__)

MODE_CALLBACK = "callback"
MODE_SDK = "sdk"

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_client: Any = None
_generation = 0
_status: dict[str, Any] = {
    "running": False,
    "error": "",
    "sdk_installed": False,
}


def _cfg(key: str, env_key: str = "", default: str = "") -> str:
    """飞书接入是部署级配置：先全局，再管理员用户级，再 .env。"""
    val = get_setting_from(key, env_key or key.upper(), None, user_id=0)
    if val not in (None, ""):
        return str(val).strip()
    try:
        from app.models.user import User

        admin = User.query.filter_by(is_admin=True).order_by(User.id).first()
        if admin is not None:
            val = get_setting(key, None, user_id=admin.id)
            if val not in (None, ""):
                return str(val).strip()
    except Exception:  # noqa: BLE001
        pass
    val = get_setting_from(key, env_key or key.upper(), default, user_id=0)
    return str(val or default).strip()


def receive_mode() -> str:
    mode = _cfg("feishu_receive_mode", "FEISHU_RECEIVE_MODE", MODE_CALLBACK)
    return MODE_SDK if mode == MODE_SDK else MODE_CALLBACK


def sdk_available() -> bool:
    try:
        import lark_oapi  # noqa: F401
        return True
    except ImportError:
        return False


def status() -> dict[str, Any]:
    st = dict(_status)
    st["mode"] = receive_mode()
    st["sdk_installed"] = sdk_available()
    st["alive"] = bool(_thread and _thread.is_alive())
    return st


def _credentials() -> tuple[str, str]:
    return _cfg("feishu_app_id", "FEISHU_APP_ID"), _cfg("feishu_app_secret", "FEISHU_APP_SECRET")


def _payload_from_sdk_event(data) -> dict:
    try:
        import lark_oapi as lark

        raw = lark.JSON.marshal(data)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:  # noqa: BLE001
        logger.exception("飞书 SDK 事件序列化失败，尝试属性读取")
    event = getattr(data, "event", None)
    message = getattr(event, "message", None)
    sender = getattr(event, "sender", None)
    sender_id = getattr(sender, "sender_id", None)
    header = getattr(data, "header", None)
    return {
        "schema": "2.0",
        "header": {
            "event_type": getattr(header, "event_type", None) or "im.message.receive_v1",
            "token": getattr(header, "token", "") or "",
        },
        "event": {
            "sender": {"sender_id": {"open_id": getattr(sender_id, "open_id", "") or ""}},
            "message": {
                "message_id": getattr(message, "message_id", "") or "",
                "chat_id": getattr(message, "chat_id", "") or "",
                "message_type": getattr(message, "message_type", "") or "",
                "content": getattr(message, "content", "") or "{}",
            },
        },
    }


def _stop_client(cli) -> None:
    if cli is None:
        return
    for name in ("stop", "close", "disconnect"):
        fn = getattr(cli, name, None)
        if callable(fn):
            try:
                fn()
                return
            except Exception:  # noqa: BLE001
                logger.exception("飞书 SDK 断开 %s 失败", name)


def _run_loop(app, app_id: str, app_secret: str, generation: int) -> None:
    global _client, _status
    try:
        import lark_oapi as lark
    except ImportError:
        _status["running"] = False
        _status["error"] = "未安装 lark-oapi，请 pip install lark-oapi"
        logger.error(_status["error"])
        return

    def on_receive(data) -> None:
        if generation != _generation:
            return
        from app.services.feishu_inbound import dispatch, parse_message

        try:
            payload = _payload_from_sdk_event(data)
            msg = parse_message(payload)
            if msg is None:
                return
            dispatch(app, msg)
        except Exception:  # noqa: BLE001
            logger.exception("飞书 SDK 处理消息失败")

    try:
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_receive)
            .build()
        )
        cli = lark.ws.Client(
            app_id, app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        with _lock:
            if generation != _generation:
                return
            _client = cli
            _status["running"] = True
            _status["error"] = ""
        logger.info("飞书 SDK 长连接启动中…")
        cli.start()
    except Exception as e:  # noqa: BLE001
        logger.exception("飞书 SDK 长连接退出")
        _status["error"] = str(e)[:300]
    finally:
        if generation == _generation:
            _status["running"] = False
            _client = None


def stop() -> None:
    """停止长连接（尽力而为；部分 SDK 版本 start() 阻塞且无 stop，需重启进程）。"""
    global _generation, _thread, _client
    with _lock:
        _generation += 1
        cli = _client
        _client = None
        _status["running"] = False
    _stop_client(cli)
    _thread = None


def start_if_needed(app) -> None:
    """按当前配置启动或停止长连接。"""
    global _thread
    if receive_mode() != MODE_SDK:
        stop()
        _status["error"] = ""
        return
    if not sdk_available():
        _status["running"] = False
        _status["error"] = "未安装 lark-oapi，请执行 pip install lark-oapi 后重启"
        logger.warning(_status["error"])
        return
    app_id, app_secret = _credentials()
    if not app_id or not app_secret:
        stop()
        _status["error"] = "未配置 App ID / App Secret"
        return

    stop()
    gen = _generation
    app_obj = app._get_current_object() if hasattr(app, "_get_current_object") else app
    t = threading.Thread(
        target=_run_loop,
        args=(app_obj, app_id, app_secret, gen),
        name="feishu-ws",
        daemon=True,
    )
    _thread = t
    t.start()
    logger.info("飞书 SDK 长连接线程已启动")


def restart(app) -> None:
    start_if_needed(app)
