"""飞书事件 HTTP 回调入口。

接入方式由设置「飞书机器人 → 接入方式」决定：
- callback：本路由接收开放平台 Webhook（需公网 HTTPS）
- sdk：官方 SDK 长连接收事件（见 feishu_ws.py）；本路由仍处理 URL 验证握手，
  但忽略消息事件，避免与长连接重复处理
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from flask import Blueprint, current_app, jsonify, request

from app.extensions import csrf
from app.services.feishu_inbound import dispatch, parse_message
from app.services.feishu_ws import MODE_SDK, receive_mode
from app.services.settings_service import get_setting

logger = logging.getLogger(__name__)

bp = Blueprint("feishu", __name__, url_prefix="/feishu")


def _decrypt(encrypt: str) -> dict:
    """解密飞书事件（AES-256-CBC）。"""
    key = get_setting("feishu_event_encrypt_key", "") or ""
    if not key:
        raise ValueError("收到加密事件但未配置加密密钥")
    raw = base64.b64decode(encrypt)
    iv, ciphertext = raw[:16], raw[16:]
    aes_key = hashlib.sha256(key.encode("utf-8")).digest()
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpad = PKCS7(algorithms.AES.block_size).unpadder()
    plain = unpad.update(padded) + unpad.finalize()
    return json.loads(plain.decode("utf-8"))


@bp.route("/event", methods=["POST"])
@csrf.exempt
def event():
    """飞书事件订阅 HTTP 回调入口。"""
    payload = request.get_json(silent=True) or {}

    encrypted = bool(payload.get("encrypt"))
    if encrypted:
        try:
            payload = _decrypt(payload["encrypt"])
        except Exception as e:  # noqa: BLE001
            logger.warning("飞书事件解密失败: %s", e)
            return jsonify({"code": 1, "msg": "decrypt failed"}), 400

    token = (get_setting("feishu_event_token", "") or "").strip()
    has_key = bool(get_setting("feishu_event_encrypt_key", "") or "")
    if not (token or has_key):
        logger.warning("飞书事件被拒绝：未配置验证令牌/加密密钥")
        return jsonify({"code": 1, "msg": "feishu token/encrypt key not configured"}), 403
    if has_key and not encrypted:
        logger.warning("飞书事件被拒绝：已配置加密密钥但事件未加密")
        return jsonify({"code": 1, "msg": "encryption required"}), 403
    provided = ((payload.get("header") or {}).get("token")
                or payload.get("token") or "").strip()
    if token and provided != token:
        logger.warning("飞书事件被拒绝：token 不匹配")
        return jsonify({"code": 1, "msg": "bad token"}), 403

    if payload.get("type") == "url_verification":
        return jsonify({"challenge": payload.get("challenge", "")})

    if receive_mode() == MODE_SDK:
        logger.debug("SDK 长连接模式下忽略 HTTP 消息事件")
        return jsonify({"code": 0})

    msg = parse_message(payload)
    if msg is None:
        return jsonify({"code": 0})
    dispatch(current_app._get_current_object(), msg)
    return jsonify({"code": 0})
