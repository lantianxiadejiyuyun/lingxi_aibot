"""飞书入站消息处理（文本 / 图片 → AI 回复），供 HTTP 回调与 SDK 长连接共用。"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import deque
from pathlib import Path

from flask import current_app

from app.services.settings_service import get_own_setting

logger = logging.getLogger(__name__)

_seen: deque = deque(maxlen=200)
_seen_lock = threading.Lock()
_conversation_map_lock = threading.Lock()
_dispatch_lock = threading.Lock()
_pending: dict[tuple[int, str, str], deque] = {}
_pending_ids: set[str] = set()


def parse_message(payload: dict) -> dict | None:
    """从 v1 / v2 事件结构中提取文本或图片消息。

    返回 {"type": "text"|"image", ...}；非文本/图片返回 None。
    """
    header = payload.get("header") or {}
    event_type = header.get("event_type") or (payload.get("event") or {}).get("type", "")
    if event_type != "im.message.receive_v1":
        return None

    event = payload.get("event") or {}
    message = event.get("message") or {}
    msg_type = message.get("message_type") or ""
    chat_id = message.get("chat_id") or ""
    message_id = message.get("message_id") or ""
    if not chat_id or not message_id:
        return None
    sender = event.get("sender") or {}
    sender_id = sender.get("sender_id") or {}
    open_id = (sender_id.get("open_id") or "").strip()
    try:
        content = json.loads(message.get("content") or "{}")
    except (TypeError, ValueError):
        content = {}

    if msg_type == "image":
        image_key = content.get("image_key") or ""
        if not image_key:
            return None
        return {"type": "image", "chat_id": chat_id,
                "message_id": message_id, "image_key": image_key, "open_id": open_id}
    if msg_type and msg_type != "text":
        return None
    text = content.get("text_without_bot_mentions") or content.get("text") or ""
    return {"type": "text", "chat_id": chat_id,
            "message_id": message_id, "text": str(text).strip(), "open_id": open_id}


def mark_seen(message_id: str) -> bool:
    """记录 message_id；已见过返回 False（跳过），首次返回 True。"""
    if not message_id:
        return False
    with _seen_lock:
        if message_id in _seen:
            return False
        _seen.append(message_id)
        return True


def dispatch(app, msg: dict) -> None:
    """同一发送者的图文按到达顺序处理，不同会话可以并行。"""
    if not msg or not msg.get("chat_id") or not msg.get("message_id"):
        return
    if msg.get("type") == "image":
        if not msg.get("image_key"):
            return
    elif msg.get("type") != "text" or not msg.get("text"):
        return
    key = (id(app), msg["chat_id"], msg.get("open_id", ""))
    message_id = msg["message_id"]
    with _dispatch_lock:
        # Active/queued messages must stay deduplicated even if enough unrelated
        # traffic arrives to evict them from the small completed-message cache.
        if message_id in _pending_ids or not mark_seen(message_id):
            return
        _pending_ids.add(message_id)
        if key in _pending:
            _pending[key].append(dict(msg))
            return
        _pending[key] = deque([dict(msg)])
        try:
            threading.Thread(target=_drain_messages, args=(app, key), daemon=True).start()
        except Exception:
            _pending.pop(key, None)
            _pending_ids.discard(message_id)
            with _seen_lock:
                if message_id in _seen:
                    _seen.remove(message_id)
            raise


def _drain_messages(app, key: tuple[int, str, str]) -> None:
    while True:
        with _dispatch_lock:
            queue = _pending[key]
            if not queue:
                del _pending[key]
                return
            msg = queue.popleft()
        try:
            if msg["type"] == "image":
                _process_image(app, msg["chat_id"], msg["message_id"],
                               msg["image_key"], msg.get("open_id", ""))
            else:
                _process_text(app, msg["chat_id"], msg["text"], msg.get("open_id", ""))
        except Exception:  # noqa: BLE001 — a failed item must not strand later messages
            logger.exception("飞书队列消息处理失败 chat=%s", msg["chat_id"])
        finally:
            with _dispatch_lock:
                with _seen_lock:
                    if msg["message_id"] in _seen:
                        _seen.remove(msg["message_id"])
                    _seen.append(msg["message_id"])
                _pending_ids.discard(msg["message_id"])


def _send_progress(chat_id: str, text: str) -> bool:
    """进度消息发送失败不应中断模型生成或工具执行。"""
    from app.services.channels.feishu_app import send_text

    if not text:
        return False
    try:
        send_text(chat_id, text)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("飞书进度消息发送失败 chat=%s", chat_id, exc_info=True)
        return False


def _send_generated_images(chat_id: str, image_ids: list[int]) -> None:
    from app.services import image_service
    from app.services.channels.feishu_app import send_image

    image_dir = Path(current_app.config["IMAGE_DIR"])
    for iid in image_ids:
        asset = image_service.get_image(iid)
        if asset is None:
            continue
        path = image_dir / asset.file_path
        if not path.exists():
            continue
        try:
            send_image(chat_id, path.read_bytes())
        except Exception:  # noqa: BLE001
            logger.exception("飞书发图失败 chat=%s image=%s", chat_id, iid)


def _resolve_feishu_user(open_id: str):
    from app.models.user import User

    if not open_id:
        return None
    return User.query.filter_by(feishu_open_id=open_id).first()


def _unbound_reply(chat_id: str, open_id: str) -> None:
    from app.services.channels.feishu_app import send_text

    oid = open_id or "（未识别到 open_id）"
    send_text(
        chat_id,
        "🔒 你的飞书身份尚未绑定到灵犀账号，无法为你服务。\n\n"
        f"你的飞书 open_id：{oid}\n"
        "请联系管理员：进入「设置 → 用户管理」，把该 open_id 绑定到你的账号即可。",
    )


def _get_or_create_conversation(user, chat_id: str):
    """Text and images use the same user-owned chat mapping."""
    from app.extensions import db
    from app.models.conversation import Conversation
    from app.services.settings_service import set_setting

    with _conversation_map_lock:
        mapping = get_own_setting("feishu_chat_map", {}, user_id=user.id)
        mapping = dict(mapping) if isinstance(mapping, dict) else {}
        conv_id = mapping.get(chat_id)
        conv = db.session.get(Conversation, conv_id) if isinstance(conv_id, int) else None
        if conv is not None and conv.user_id == user.id:
            return conv
        conv = Conversation(title=f"飞书-{chat_id[:12]}", user_id=user.id)
        db.session.add(conv)
        db.session.commit()
        mapping[chat_id] = conv.id
        set_setting("feishu_chat_map", mapping, user_id=user.id)
        return conv


def _process_text(app, chat_id: str, text: str, open_id: str = "") -> None:
    from app.utils.scoping import user_scope

    user_id = 0
    with app.app_context():
        from app.ai.executor import run_chat
        from app.services.channels.feishu_app import send_text

        try:
            with app.test_request_context():
                from flask_login import login_user

                user = _resolve_feishu_user(open_id)
                if user is None:
                    logger.warning("飞书消息忽略：发送者未绑定 open_id=%s", open_id)
                    _unbound_reply(chat_id, open_id)
                    return
                user_id = user.id
                login_user(user)

                conv = _get_or_create_conversation(user, chat_id)

                image_ids: list[int] = []
                final_text, err, ack_text = "", "", ""
                chunks: list[str] = []
                # Feishu group messages can prefix slash commands with bot mentions.
                command_text = re.sub(r'^\s*(?:@_user_\d+\s*)+', '', text)
                for ev in run_chat(conv, command_text, user):
                    if ev[0] == "ack":
                        if _send_progress(chat_id, ev[1]):
                            ack_text = ev[1]
                    elif ev[0] == "delta":
                        chunks.append(ev[1])
                    elif ev[0] == "done":
                        final_text = ev[1]
                    elif ev[0] == "error":
                        err = ev[1]
                    elif ev[0] == 'notice':
                        _send_progress(chat_id, ev[1])
                    elif ev[0] == "tool" and ev[1].get("name") in ("generate_image", "edit_image"):
                        m = re.search(r'"id"\s*:\s*(\d+)', ev[1].get("result") or "")
                        if m:
                            image_id = int(m.group(1))
                            if image_id not in image_ids:
                                image_ids.append(image_id)

                reply = final_text or "".join(chunks)
                if err:
                    failure = f"🤖 本次回复未完成：{err}"
                    reply = f"{reply}\n\n{failure}" if reply else failure
                reply_clean = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', reply).strip()
                if ack_text:
                    from app.ai.prompts import strip_leading_ack

                    reply_clean = strip_leading_ack(reply_clean, ack_text)

                if reply_clean:
                    from app.utils.netinfo import feishu_sdk_page_warning, reply_looks_like_page

                    if reply_looks_like_page(reply_clean):
                        note = feishu_sdk_page_warning()
                        if note:
                            reply_clean = reply_clean + "\n\n⚠️ " + note
                    send_text(chat_id, reply_clean)
                elif not image_ids:
                    send_text(chat_id, "🤖 未能生成有效回复，请在网页查看会话记录及操作结果后继续。")

                if image_ids:
                    _send_generated_images(chat_id, image_ids)

                logger.info("飞书消息已回复 chat=%s", chat_id)
        except Exception:  # noqa: BLE001
            logger.exception("飞书消息处理失败 chat=%s", chat_id)
            with user_scope(user_id):
                _send_progress(chat_id, "🤖 本次消息处理或回复发送失败，请在网页查看会话记录及操作结果后继续。")


def _process_image(app, chat_id: str, message_id: str, image_key: str, open_id: str = "") -> None:
    from app.services.channels.feishu_app import download_image_resource, send_text
    from app.services.image_input_service import receive_image
    from app.utils.scoping import user_scope

    with app.app_context():
        try:
            with app.test_request_context():
                from flask_login import login_user

                user = _resolve_feishu_user(open_id)
                if user is None:
                    logger.warning("飞书图片忽略：发送者未绑定 open_id=%s", open_id)
                    _unbound_reply(chat_id, open_id)
                    return
                login_user(user)

                # Keep the sender's scope for credentials, active model and history.
                with user_scope(user.id):
                    conv = _get_or_create_conversation(user, chat_id)
                    data = download_image_resource(message_id, image_key)
                    result = receive_image(user, data, conversation=conv,
                                           on_identify=lambda: _send_progress(chat_id, "🔍 正在识别图片…"))
                    send_text(chat_id, result["reply"])
        except Exception as e:  # noqa: BLE001
            logger.exception("飞书图片处理失败 chat=%s", chat_id)
            try:
                send_text(chat_id, f"🖼️ 图片处理失败：{str(e)[:100]}")
            except Exception:  # noqa: BLE001
                logger.exception("飞书图片失败回复也失败 chat=%s", chat_id)
