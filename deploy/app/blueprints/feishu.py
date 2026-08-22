"""飞书事件回调：接收机器人消息（文本 + 图片）→ AI 回复 / 保存图片（应用机器人"接收"半边）。

回调地址：https://<你的域名>/feishu/event
飞书开放平台配置：自建应用 → 事件订阅 → 添加事件 im.message.receive_v1，
并填验证令牌 / 加密密钥（可选）与该回调地址。

安全：
- url_verification 握手校验 token，不匹配返回 403
- 收到消息先 200 应答（防飞书重试），AI 处理放后台线程
- 同一 message_id 去重（内存环形缓冲），避免飞书重试导致重复回复

消息类型：
- text：建/取会话 → AI 生成回复 → 发回文本；若本轮对话中 AI 调用了 generate_image /
  edit_image，则把生成的图片通过应用机器人一并发回
- image：下载图片 → 存入图片库 → 文字回复
"""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
from collections import deque
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from flask import Blueprint, current_app, jsonify, request

from app.extensions import csrf
from app.services.settings_service import get_setting

logger = logging.getLogger(__name__)

bp = Blueprint("feishu", __name__, url_prefix="/feishu")

# 已处理消息 id 去重（内存环形缓冲；单进程部署足够）
_seen: deque = deque(maxlen=200)


def _decrypt(encrypt: str) -> dict:
    """解密飞书事件（AES-256-CBC，密钥为 encrypt_key，IV 取密文前 16 字节）。"""
    key = get_setting("feishu_event_encrypt_key", "") or ""
    if not key:
        raise ValueError("收到加密事件但未配置加密密钥")
    raw = base64.b64decode(encrypt)
    iv, ciphertext = raw[:16], raw[16:]
    cipher = Cipher(algorithms.AES(key.encode("utf-8")), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpad = PKCS7(algorithms.AES.block_size).unpadder()
    plain = unpad.update(padded) + unpad.finalize()
    return json.loads(plain.decode("utf-8"))


def _parse_message(payload: dict) -> dict | None:
    """从 v1 / v2 事件结构中提取文本或图片消息。

    返回 {"type": "text", ...} 或 {"type": "image", ...}；非文本/图片返回 None。
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
    try:
        content = json.loads(message.get("content") or "{}")
    except (TypeError, ValueError):
        content = {}

    if msg_type == "image":
        image_key = content.get("image_key") or ""
        if not image_key:
            return None
        return {"type": "image", "chat_id": chat_id,
                "message_id": message_id, "image_key": image_key}
    if msg_type and msg_type != "text":
        return None  # 暂只处理文本与图片消息
    text = content.get("text_without_bot_mentions") or content.get("text") or ""
    return {"type": "text", "chat_id": chat_id,
            "message_id": message_id, "text": str(text).strip()}


def _send_generated_images(chat_id: str, image_ids: list[int]) -> None:
    """把本轮对话生成的图片通过飞书应用机器人发回（失败只记日志，不阻断）。"""
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


def _process_text(app, chat_id: str, text: str) -> None:
    """后台处理文本消息：建/取会话 → AI 生成回复 → 发回文本 + 生成的图片。"""
    from app.extensions import db
    from app.models.conversation import Conversation
    from app.models.user import User

    with app.app_context():
        from app.ai.executor import run_chat
        from app.ai.llm import LLMClient
        from app.services.channels.feishu_app import send_text

        try:
            with app.test_request_context():
                from flask_login import login_user

                user = User.query.first()
                if user is None:
                    logger.warning("飞书消息忽略：尚无用户")
                    return
                login_user(user)

                # 先建/取会话（多轮上下文），再判断 AI 是否可用
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
                    send_text(chat_id, "🤖 灵犀 尚未配置 AI（LLM_API_KEY），请先在「设置 → AI 设置」中配置后再试。")
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
                # 文本回复去掉 Markdown 图片链接（图片单独发送，避免显示裸 URL）
                reply_clean = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', reply).strip()

                if reply_clean:
                    send_text(chat_id, reply_clean)
                elif not image_ids:
                    send_text(chat_id, "🤖 已处理。")

                if image_ids:
                    _send_generated_images(chat_id, image_ids)

                logger.info("飞书消息已回复 chat=%s", chat_id)
        except Exception:  # noqa: BLE001 —— 后台线程异常只记录
            logger.exception("飞书消息处理失败 chat=%s", chat_id)


def _process_image(app, chat_id: str, message_id: str, image_key: str) -> None:
    """后台处理图片消息：下载 → 存入图片库 → （若配置视觉模型）识别 → 文字回复。"""
    from app.extensions import db
    from app.models.image import ImageAsset
    from app.services import image_service, vision_service
    from app.services.channels.feishu_app import download_image_resource, send_text

    with app.app_context():
        try:
            data = download_image_resource(message_id, image_key)
            fname = image_service.save_bytes(data)
            asset = ImageAsset(prompt="（飞书收到的图片）", file_path=fname)
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


@bp.route("/event", methods=["POST"])
@csrf.exempt  # 飞书服务器回调不带 CSRF token
def event():
    """飞书事件订阅回调入口。"""
    payload = request.get_json(silent=True) or {}

    # 加密事件先解密
    if "encrypt" in payload and payload.get("encrypt"):
        try:
            payload = _decrypt(payload["encrypt"])
        except Exception as e:  # noqa: BLE001
            logger.warning("飞书事件解密失败: %s", e)
            return jsonify({"code": 1, "msg": "decrypt failed"}), 400

    # 1. URL 验证握手
    if payload.get("type") == "url_verification":
        token = get_setting("feishu_event_token", "") or ""
        if token and payload.get("token") != token:
            return jsonify({"code": 1, "msg": "bad token"}), 403
        return jsonify({"challenge": payload.get("challenge", "")})

    # 2. 消息事件：先应答 200，再后台处理
    msg = _parse_message(payload)
    if msg is None:
        return jsonify({"code": 0})
    if msg["message_id"] in _seen:
        return jsonify({"code": 0})  # 去重：飞书重试不再处理
    _seen.append(msg["message_id"])

    app_obj = current_app._get_current_object()
    if msg["type"] == "image":
        threading.Thread(
            target=_process_image,
            args=(app_obj, msg["chat_id"], msg["message_id"], msg["image_key"]),
            daemon=True,
        ).start()
    else:
        if not msg["text"]:
            return jsonify({"code": 0})
        threading.Thread(
            target=_process_text,
            args=(app_obj, msg["chat_id"], msg["text"]),
            daemon=True,
        ).start()
    return jsonify({"code": 0})
