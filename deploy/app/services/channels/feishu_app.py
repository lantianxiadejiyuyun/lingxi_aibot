"""飞书应用机器人渠道（双向）：API 主动发送 + 接收消息事件。

- 主动发送：应用身份换取 tenant_access_token，调用 im/v1/messages 发送到默认目标
  （配置 feishu_app_target + feishu_app_target_type，支持 chat_id / open_id）
- 接收消息：见 app/blueprints/feishu.py 的 /feishu/event 回调
- 事件处理里回复消息也复用本模块的 send_text（指定 chat_id）
"""
from __future__ import annotations

import json
import logging
import time

import requests

from app.services.notify_service import BaseChannel, register_channel
from app.services.settings_service import get_setting_from

logger = logging.getLogger(__name__)

_API_BASE = "https://open.feishu.cn/open-apis"

_token_cache: dict = {"token": "", "expire_at": 0}
_token_lock = None  # 单进程单线程调度 + 请求线程，简单起见不加重锁


def _app_credentials() -> tuple[str, str]:
    app_id = get_setting_from("feishu_app_id", "FEISHU_APP_ID", "") or ""
    app_secret = get_setting_from("feishu_app_secret", "FEISHU_APP_SECRET", "") or ""
    return app_id, app_secret


def get_tenant_access_token() -> str:
    """获取 tenant_access_token（带缓存，过期前 60 秒刷新）。"""
    now = time.time()
    if _token_cache["token"] and _token_cache["expire_at"] - 60 > now:
        return _token_cache["token"]

    app_id, app_secret = _app_credentials()
    if not app_id or not app_secret:
        raise ValueError("飞书应用未配置（app_id / app_secret）")

    resp = requests.post(
        f"{_API_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    try:
        data = resp.json()
    except ValueError:
        raise ValueError(resp.text[:200]) from None
    if data.get("code") != 0:
        raise ValueError(f"获取 access_token 失败: {data.get('msg', resp.text[:200])}")
    _token_cache["token"] = data.get("tenant_access_token", "")
    _token_cache["expire_at"] = now + int(data.get("expire", 7200))
    return _token_cache["token"]


def send_text(chat_id: str, text: str, receive_id_type: str = "chat_id") -> None:
    """通过应用机器人发送文本消息到指定目标（chat_id / open_id）。"""
    token = get_tenant_access_token()
    resp = requests.post(
        f"{_API_BASE}/im/v1/messages?receive_id_type={receive_id_type}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text[:4000]}, ensure_ascii=False),
        },
        timeout=15,
    )
    try:
        data = resp.json()
    except ValueError:
        raise ValueError(resp.text[:200]) from None
    if data.get("code") != 0:
        raise ValueError(f"飞书发送失败: {data.get('msg', resp.text[:200])}")


def upload_image(image_bytes: bytes) -> str:
    """上传图片到飞书（im/v1/images），返回 image_key。"""
    token = get_tenant_access_token()
    resp = requests.post(
        f"{_API_BASE}/im/v1/images",
        headers={"Authorization": f"Bearer {token}"},
        files={"image": ("image.png", image_bytes, "image/png")},
        data={"image_type": "message"},
        timeout=30,
    )
    try:
        data = resp.json()
    except ValueError:
        raise ValueError(resp.text[:200]) from None
    if data.get("code") != 0:
        raise ValueError(f"上传图片失败: {data.get('msg', resp.text[:200])}")
    return data.get("data", {}).get("image_key", "")


def send_image(chat_id: str, image_bytes: bytes, receive_id_type: str = "chat_id") -> None:
    """上传并发送图片消息到指定目标（chat_id / open_id）。"""
    image_key = upload_image(image_bytes)
    token = get_tenant_access_token()
    resp = requests.post(
        f"{_API_BASE}/im/v1/messages?receive_id_type={receive_id_type}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "receive_id": chat_id,
            "msg_type": "image",
            "content": json.dumps({"image_key": image_key}),
        },
        timeout=15,
    )
    try:
        data = resp.json()
    except ValueError:
        raise ValueError(resp.text[:200]) from None
    if data.get("code") != 0:
        raise ValueError(f"飞书发送图片失败: {data.get('msg', resp.text[:200])}")


def download_image_resource(message_id: str, file_key: str) -> bytes:
    """下载飞书消息里的图片资源（im/v1/messages/{mid}/resources/{key}?type=image）。"""
    token = get_tenant_access_token()
    resp = requests.get(
        f"{_API_BASE}/im/v1/messages/{message_id}/resources/{file_key}",
        params={"type": "image"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise ValueError(f"下载图片失败 HTTP {resp.status_code}: {resp.text[:150]}")
    return resp.content


@register_channel
class FeishuAppChannel(BaseChannel):
    """飞书应用机器人渠道：主动发送到设置页配置的默认目标（支持文本 + 图片）。"""

    name = "feishu_app"
    display_name = "飞书机器人(应用)"

    @property
    def configured(self) -> bool:
        app_id, app_secret = _app_credentials()
        return bool(app_id and app_secret)

    def send(self, title: str, body: str, image_bytes: bytes | None = None) -> None:
        target = get_setting_from("feishu_app_target", "", "") or ""
        if not target:
            raise ValueError("飞书应用机器人未配置默认发送目标（chat_id / open_id）")
        target_type = get_setting_from("feishu_app_target_type", "", "chat_id") or "chat_id"
        if target_type not in ("chat_id", "open_id"):
            target_type = "chat_id"
        if image_bytes:
            send_image(target, image_bytes, receive_id_type=target_type)
            if title or body:
                send_text(target, f"{title}\n{body}", receive_id_type=target_type)
        else:
            send_text(target, f"{title}\n{body}", receive_id_type=target_type)
