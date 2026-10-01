"""飞书官方 SDK 长连接（WebSocket）：无需公网回调地址，进程主动连开放平台收事件。

后台「飞书机器人 → 接入方式」选「官方 SDK 长连接」后启动。
飞书开放平台事件订阅需选择「使用长连接接收事件」。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Optional

from app.services.settings_service import get_setting, get_setting_from

logger = logging.getLogger(__name__)

MODE_CALLBACK = "callback"
MODE_SDK = "sdk"

_lock = threading.Lock()
_lifecycle_lock = threading.RLock()
_thread: Optional[threading.Thread] = None
_client: Any = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_generation = 0
_STOP_TIMEOUT = 8
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
    with _lock:
        st = dict(_status)
        st["alive"] = bool(_thread and _thread.is_alive())
    st["mode"] = receive_mode()
    st["sdk_installed"] = sdk_available()
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


def _cancel_tasks(loop: asyncio.AbstractEventLoop) -> None:
    # Runs on the owner thread; cancelling start() also interrupts reconnect waits.
    for task in asyncio.all_tasks(loop):
        task.cancel()


def _interrupt_loop(loop: asyncio.AbstractEventLoop) -> None:
    with _lock:
        if _loop is not loop:
            return  # Cleanup is already running; do not cancel its disconnect.
    _cancel_tasks(loop)
    # start() runs several coroutines in sequence. A stop landing between them
    # must also cancel the next one, until the worker reaches its cleanup block.
    loop.call_later(0.05, _interrupt_loop, loop)


def _run_loop(app, app_id: str, app_secret: str, generation: int) -> None:
    global _client, _loop, _thread
    cli = None
    loop = None
    try:
        import lark_oapi as lark
        import lark_oapi.ws.client as sdk_ws
    except ImportError:
        with _lock:
            if generation == _generation:
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
        # lark-oapi 1.x uses a module-level loop, and Client.start() has no stop().
        # Only replace it once the previous worker has fully exited. Setting the
        # thread's loop first also binds SDK cache tasks to this same loop.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        sdk_ws.loop = loop
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
            _loop = loop
            _status["running"] = True
            _status["error"] = ""
        logger.info("飞书 SDK 长连接启动中…")
        cli.start()
    except asyncio.CancelledError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.exception("飞书 SDK 长连接退出")
        with _lock:
            if generation == _generation:
                _status["error"] = str(e)[:300]
    finally:
        with _lock:
            if _loop is loop:
                _loop = None
        if loop is not None:
            try:
                if cli is not None:
                    cli._auto_reconnect = False
                _cancel_tasks(loop)
                pending = asyncio.all_tasks(loop)
                if pending:
                    completed, pending = loop.run_until_complete(asyncio.wait(pending, timeout=3))
                    for task in completed:
                        if not task.cancelled():
                            task.exception()
                    if pending:
                        logger.warning("飞书 SDK 有 %s 个任务未及时取消", len(pending))
                if cli is not None:
                    loop.run_until_complete(asyncio.wait_for(cli._disconnect(), timeout=3))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                logger.exception("飞书 SDK 长连接清理失败")
            finally:
                loop.close()
                asyncio.set_event_loop(None)
        with _lock:
            if _thread is threading.current_thread():
                _thread = None
                _loop = None
                _client = None
                _status["running"] = False


def _stop_locked() -> bool:
    """Called with the lifecycle lock; never start a second SDK loop concurrently."""
    global _generation, _thread, _client
    with _lock:
        _generation += 1
        thread, loop = _thread, _loop
        _status["running"] = False
    if loop is not None and not loop.is_closed():
        try:
            loop.call_soon_threadsafe(_interrupt_loop, loop)
        except RuntimeError:
            pass  # Worker finished between the state read and the stop request.
    if thread is not None and thread.is_alive():
        if thread is not threading.current_thread():
            thread.join(timeout=_STOP_TIMEOUT)
        if thread.is_alive():
            with _lock:
                _status["error"] = "旧飞书连接尚未退出，请稍后重试或重启服务"
            return False
    with _lock:
        _thread = None
        _client = None
    return True


def stop() -> None:
    """Cancel SDK tasks and wait for the owning thread to close its connection."""
    with _lifecycle_lock:
        _stop_locked()


def start_if_needed(app) -> None:
    """按当前配置启动或停止长连接。"""
    with _lifecycle_lock:
        _start_locked(app)


def _start_locked(app) -> None:
    global _thread
    if not _stop_locked():
        return
    if receive_mode() != MODE_SDK:
        _status["error"] = ""
        return
    if not sdk_available():
        _status["running"] = False
        _status["error"] = "未安装 lark-oapi，请执行 pip install lark-oapi 后重启"
        logger.warning(_status["error"])
        return
    app_id, app_secret = _credentials()
    if not app_id or not app_secret:
        _status["error"] = "未配置 App ID / App Secret"
        return

    gen = _generation
    app_obj = app._get_current_object() if hasattr(app, "_get_current_object") else app
    t = threading.Thread(
        target=_run_loop,
        args=(app_obj, app_id, app_secret, gen),
        name="feishu-ws",
        daemon=True,
    )
    with _lock:
        _thread = t
    t.start()
    logger.info("飞书 SDK 长连接线程已启动")


def restart(app) -> None:
    start_if_needed(app)
