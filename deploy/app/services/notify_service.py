"""通知服务：可插拔渠道注册 + 统一发送入口。

新增渠道只需两步：
  1. 在 app/services/channels/ 下新建模块，继承 BaseChannel 并用 @register_channel 注册
  2. 在设置页填入该渠道的配置（settings 表，缺省回退 .env）

渠道类规范：
  - name: 唯一英文标识（对应 Notification.channel）
  - display_name: 中文名
  - configured: 是否已配置（未配置时 notify 会记录失败）
  - send(title, body): 发送成功静默返回，失败抛异常（异常信息会记录到 Notification.error）
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Optional

from app.extensions import db
from app.models.notification import (
    CHANNEL_INAPP, STATUS_FAILED, STATUS_PENDING, STATUS_SENT, Notification,
)
from app.services import settings_service
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)

CHANNELS: dict[str, type] = {}


class BaseChannel:
    name: str = ""
    display_name: str = ""

    @property
    def configured(self) -> bool:
        return False

    def send(self, title: str, body: str, image_bytes: bytes | None = None) -> None:
        """发送通知。image_bytes 为可选的图片原始字节（各渠道按能力处理）。"""
        raise NotImplementedError


def register_channel(cls):
    """注册渠道类。"""
    CHANNELS[cls.name] = cls
    return cls


@register_channel
class InAppChannel(BaseChannel):
    name = CHANNEL_INAPP
    display_name = "站内通知"

    @property
    def configured(self) -> bool:
        return True

    def send(self, title: str, body: str, image_bytes: bytes | None = None) -> None:
        pass  # 数据库记录即视为送达


def load_channels() -> list[str]:
    """自动导入 app.services.channels 下所有模块，触发渠道注册。"""
    import app.services.channels as pkg

    loaded = []
    for mod in pkgutil.iter_modules(pkg.__path__):
        try:
            importlib.import_module(f"{pkg.__name__}.{mod.name}")
            loaded.append(mod.name)
        except Exception:  # noqa: BLE001
            logger.exception("通知渠道模块加载失败: %s", mod.name)
    return loaded


def get_channel(name: str) -> Optional[BaseChannel]:
    cls = CHANNELS.get(name)
    return cls() if cls else None


def available_channels() -> list[dict]:
    return [
        {"name": cls.name, "display_name": cls.display_name, "configured": cls().configured}
        for cls in CHANNELS.values()
    ]


def default_channels() -> list[str]:
    """默认推送渠道（settings.default_channels，缺省回退 .env DEFAULT_CHANNELS）。"""
    raw = settings_service.get_setting_from("default_channels", "DEFAULT_CHANNELS", "inapp")
    if isinstance(raw, str):
        raw = [c.strip() for c in raw.split(",") if c.strip()]
    if not raw:
        raw = [CHANNEL_INAPP]
    return [c for c in raw if c in CHANNELS or c == CHANNEL_INAPP]


# 通知场景 → settings 键（应用场景渠道，统一在设置页「通知渠道」配置）
SCENE_KEYS = {
    "briefing": "notify_briefing_channels",   # 早安/午间/晚间简报
    "report": "notify_report_channels",       # 周报/月报
    "backup": "notify_backup_channels",       # 备份完成
    "cleanup": "notify_cleanup_channels",     # 每周整理
    "reminder": "notify_reminder_channels",   # 事件/任务到期提醒
}


def scene_channels(scene: str) -> Optional[list[str]]:
    """某应用场景配置的渠道（settings 键），未配置返回 None（跟随全局默认）。"""
    key = SCENE_KEYS.get(scene)
    if not key:
        return None
    raw = settings_service.get_setting(key)
    if raw is None and scene == "backup":
        raw = settings_service.get_setting("backup_channel")  # 兼容旧键
    if not raw:
        return None
    if isinstance(raw, str):
        raw = [c.strip() for c in raw.split(",") if c.strip()]
    raw = [c for c in raw if c in CHANNELS or c == CHANNEL_INAPP]
    return raw or None


def notify_for(scene: str, title: str, body: str, explicit: Optional[list | str] = None,
               image_bytes: bytes | None = None):
    """按场景发送通知：explicit（任务页覆盖）> 场景渠道（settings）> 全局默认。

    供各应用动作（简报/报告/备份/整理/提醒）统一调用，避免每个功能单独配渠道。
    """
    channels = explicit if explicit is not None else scene_channels(scene)
    if not channels:
        channels = default_channels()
    return notify(title, body, channels, image_bytes=image_bytes)


def notify(title: str, body: str, channels: Optional[list[str] | str] = None,
           image_bytes: bytes | None = None) -> list[Notification]:
    """发送通知到指定渠道（默认渠道列表），每渠道生成一条记录。

    :param image_bytes: 可选图片字节，渠道按其能力发送（飞书应用机器人可上传发图）
    :return: 生成的 Notification 记录列表（无论成败均落库）
    """
    if channels is None:
        channels = default_channels()
    if isinstance(channels, str):
        channels = [c.strip() for c in channels.split(",") if c.strip()]

    records = []
    for name in channels:
        rec = Notification(channel=name, title=title, body=body, status=STATUS_PENDING)
        db.session.add(rec)
        db.session.flush()
        ch = get_channel(name)
        try:
            if ch is None:
                raise ValueError(f"未知渠道: {name}")
            if not ch.configured:
                raise ValueError(f"渠道 {ch.display_name} 未配置")
            ch.send(title, body, image_bytes=image_bytes)
            rec.status = STATUS_SENT
            rec.sent_at = utcnow()
        except Exception as e:  # noqa: BLE001
            rec.status = STATUS_FAILED
            rec.error = str(e)[:500]
            logger.warning("渠道 %s 发送失败: %s", name, e)
        records.append(rec)
    db.session.commit()
    return records
