"""飞书入站消息处理（文本 / 图片 → AI 回复），供 HTTP 回调与 SDK 长连接共用。"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import deque
from pathlib import Path

from flask import current_app

from app.services.settings_service import get_setting

logger = logging.getLogger(__name__)

_seen: deque = deque(maxlen=200)
_seen_lock = threading.Lock()


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
    """后台线程处理一条已解析消息。"""
    if not msg:
        return
    if not mark_seen(msg.get("message_id") or ""):
        return
    if msg.get("type") == "image":
        threading.Thread(
            target=_process_image,
            args=(app, msg["chat_id"], msg["message_id"], msg["image_key"],
                  msg.get("open_id", "")),
            daemon=True,
        ).start()
        return
    if not msg.get("text"):
        return
    threading.Thread(
        target=_process_text,
        args=(app, msg["chat_id"], msg["text"], msg.get("open_id", "")),
        daemon=True,
    ).start()


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


def _process_text(app, chat_id: str, text: str, open_id: str = "") -> None:
    from app.extensions import db
    from app.models.conversation import Conversation

    with app.app_context():
        from app.ai.executor import run_chat
        from app.ai.llm import LLMClient
        from app.services.channels.feishu_app import send_text

        try:
            with app.test_request_context():
                from flask_login import login_user

                user = _resolve_feishu_user(open_id)
                if user is None:
                    logger.warning("飞书消息忽略：发送者未绑定 open_id=%s", open_id)
                    _unbound_reply(chat_id, open_id)
                    return
                login_user(user)

                mapping = get_setting("feishu_chat_map", {}) or {}
                conv = None
                if mapping.get(chat_id):
                    conv = db.session.get(Conversation, mapping[chat_id])
                if conv is None:
                    conv = Conversation(title=f"飞书-{chat_id[:12]}")
                    db.session.add(conv)
                    db.session.commit()
                    mapping[chat_id] = conv.id
                    from app.services.settings_service import set_setting

                    set_setting("feishu_chat_map", mapping)

                if not LLMClient().is_configured:
                    send_text(chat_id, "🤖 灵犀：你的账号尚未配置 AI。请打开网页「设置 → 模型与人设」，填写你自己的 API Key（OpenAI 兼容或 Anthropic）。")
                    return

                image_ids: list[int] = []
                final_text, err = "", ""
                for ev in run_chat(conv, text, user):
                    if ev[0] == "done":
                        final_text = ev[1]
                    elif ev[0] == "error":
                        err = ev[1]
                    elif ev[0] == "tool" and ev[1].get("name") in ("generate_image", "edit_image"):
                        m = re.search(r'"id"\s*:\s*(\d+)', ev[1].get("result") or "")
                        if m:
                            image_ids.append(int(m.group(1)))

                reply = final_text or f"🤖 处理失败：{err}"
                reply_clean = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', reply).strip()

                if reply_clean:
                    from app.utils.netinfo import feishu_sdk_page_warning, reply_looks_like_page

                    note = feishu_sdk_page_warning()
                    if note and reply_looks_like_page(reply_clean):
                        reply_clean = reply_clean + "\n\n⚠️ " + note
                    send_text(chat_id, reply_clean)
                elif not image_ids:
                    send_text(chat_id, "🤖 已处理。")

                if image_ids:
                    _send_generated_images(chat_id, image_ids)

                logger.info("飞书消息已回复 chat=%s", chat_id)
        except Exception:  # noqa: BLE001
            logger.exception("飞书消息处理失败 chat=%s", chat_id)


def _process_image(app, chat_id: str, message_id: str, image_key: str, open_id: str = "") -> None:
    from app.extensions import db
    from app.models.image import ImageAsset
    from app.services import image_service, vision_service
    from app.services.channels.feishu_app import download_image_resource, send_text

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

            data = download_image_resource(message_id, image_key)
            fname = image_service.save_bytes(data)
            asset = ImageAsset(user_id=user.id, prompt="（飞书收到的图片）", file_path=fname)
            db.session.add(asset)
            db.session.commit()

            if vision_service.is_configured():
                send_text(chat_id, "🔍 正在识别图片…")
                try:
                    desc = vision_service.describe_image(data)
                    reply = f"🖼️ 已收到图片并保存（ID #{asset.id}）。识别结果：\n{desc}"
                except Exception as e:  # noqa: BLE001
                    logger.warning("视觉识别失败 chat=%s: %s", chat_id, e)
                    reply = (f"🖼️ 已保存图片（ID #{asset.id}），但识别失败：{str(e)[:150]}")
            else:
                reply = (f"🖼️ 已收到图片并保存到图片库（ID #{asset.id}）。"
                         f"尚未配置视觉模型，暂无法识图。")
            send_text(chat_id, reply)
        except Exception as e:  # noqa: BLE001
            logger.exception("飞书图片处理失败 chat=%s", chat_id)
            try:
                send_text(chat_id, f"🖼️ 图片处理失败：{str(e)[:100]}")
            except Exception:  # noqa: BLE001
                logger.exception("飞书图片失败回复也失败 chat=%s", chat_id)
