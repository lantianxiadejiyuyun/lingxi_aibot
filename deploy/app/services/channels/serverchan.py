"""Server酱 通知渠道：通过 SendKey 推送消息到微信（https://sct.ftqq.com）。"""
from __future__ import annotations

import requests

from app.services.notify_service import BaseChannel, register_channel
from app.services.settings_service import get_setting_from


@register_channel
class ServerChanChannel(BaseChannel):
    name = "serverchan"
    display_name = "Server酱"

    @property
    def configured(self) -> bool:
        return bool(get_setting_from("sc_key", "SC_KEY", ""))

    def send(self, title: str, body: str, image_bytes: bytes | None = None) -> None:
        # Server酱 desp 支持 Markdown，可内嵌 ![](公网图片URL)；但本地图片字节无公网地址，
        # 因此这里仅发送文本（image_bytes 忽略）。如需发图请把图片设为公开并传 URL。
        key = get_setting_from("sc_key", "SC_KEY", "")
        if not key:
            raise ValueError("Server酱 SendKey 未配置")
        resp = requests.post(
            f"https://sctapi.ftqq.com/{key}.send",
            data={"title": title, "desp": body},
            timeout=10,
        )
        try:
            data = resp.json()
        except ValueError:
            raise ValueError(resp.text[:200]) from None
        if data.get("code") != 0:
            raise ValueError(resp.text[:200])
