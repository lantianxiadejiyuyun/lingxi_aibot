"""飞书通知渠道：自定义机器人 Webhook（支持加签，文本 + 可选图片）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

import requests

from app.services.notify_service import BaseChannel, register_channel
from app.services.settings_service import get_setting_from


@register_channel
class FeishuChannel(BaseChannel):
    name = "feishu"
    display_name = "飞书"

    @property
    def configured(self) -> bool:
        return bool(get_setting_from("feishu_webhook_url", "FEISHU_WEBHOOK_URL", ""))

    def _signed(self, payload: dict) -> dict:
        """飞书加签：string_to_sign 为密钥、空消息计算 HMAC-SHA256 后 base64。"""
        secret = get_setting_from("feishu_secret", "FEISHU_SECRET", "")
        if secret:
            timestamp = str(int(time.time()))
            string_to_sign = f"{timestamp}\n{secret}"
            sign = base64.b64encode(
                hmac.new(string_to_sign.encode(), digestmod=hashlib.sha256).digest()
            ).decode()
            payload["timestamp"] = timestamp
            payload["sign"] = sign
        return payload

    def _post(self, url: str, payload: dict) -> None:
        resp = requests.post(url, json=self._signed(payload), timeout=10)
        try:
            data = resp.json()
        except ValueError:
            raise ValueError(resp.text[:200]) from None
        if data.get("code") != 0:
            raise ValueError(resp.text[:200])

    def send(self, title: str, body: str, image_bytes: bytes | None = None) -> None:
        url = get_setting_from("feishu_webhook_url", "FEISHU_WEBHOOK_URL", "")
        if not url:
            raise ValueError("飞书 Webhook 地址未配置")

        # 图片消息：自定义机器人无法直接上传，需借用应用机器人上传拿 image_key
        if image_bytes:
            try:
                from app.services.channels.feishu_app import upload_image

                image_key = upload_image(image_bytes)
                self._post(url, {"msg_type": "image", "content": {"image_key": image_key}})
                return
            except Exception:  # noqa: BLE001 —— 上传/发图失败则退回文本
                pass

        self._post(url, {"msg_type": "text", "content": {"text": f"{title}\n{body}"}})
