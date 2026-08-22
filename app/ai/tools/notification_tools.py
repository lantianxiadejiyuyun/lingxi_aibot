"""AI 工具：发送通知 / 查询通知记录。"""
from __future__ import annotations

from flask import current_app
from flask_login import current_user

from app.ai.registry import register_tool
from app.models.notification import Notification
from app.services.notify_service import CHANNELS, GROUP_PREFIX, list_notify_groups, notify
from app.utils.timeutil import fmt_dt, get_tz


def _tz():
    """工具调用上下文时区：优先当前用户，缺省应用配置。"""
    name = getattr(current_user, "timezone", None) \
        or current_app.config.get("APP_TIMEZONE", "Asia/Shanghai")
    return get_tz(name)


def _parse_channels(channels) -> list[str] | None:
    """逗号分隔字符串或列表 → 渠道名/通知组引用列表；None/空 → None（走默认渠道）。"""
    if channels is None:
        return None
    if isinstance(channels, str):
        items = [c.strip() for c in channels.split(",") if c.strip()]
    elif isinstance(channels, (list, tuple)):
        items = [str(c).strip() for c in channels if str(c).strip()]
    else:
        raise ValueError("channels 必须是逗号分隔字符串或列表")
    if not items:
        return None
    group_names = set(list_notify_groups())
    unknown = [
        c for c in items
        if c not in CHANNELS
        and not (c.startswith(GROUP_PREFIX) and c[len(GROUP_PREFIX):] in group_names)
    ]
    if unknown:
        valid = sorted(CHANNELS) + [f"{GROUP_PREFIX}{n}" for n in sorted(group_names)]
        raise ValueError(
            f"未知渠道或通知组: {', '.join(unknown)}（可选: {', '.join(valid)}）")
    return items


@register_tool(
    "send_notification",
    "发送通知。当用户要求发送通知/提醒/推送（如设定提醒、定时推送、把信息发到微信或飞书）时调用。"
    "title 为通知标题（必填）；body 为正文内容；channels 为目标渠道，逗号分隔字符串或列表，"
    "可选值: inapp（站内通知）、serverchan（Server酱推送到微信）、feishu（飞书机器人）、"
    "feishu_app（飞书应用机器人），也可填「group:组名」引用设置页自定义的通知组（一次联动组内多个渠道），"
    "省略时使用设置页配置的默认渠道；"
    "image_id 为可选图片 ID（从 list_images 获取），填写后把该图片随通知一起发送"
    "（飞书/飞书应用机器人支持发图，Server酱与站内仅文本）。返回每条渠道的发送结果（渠道/状态/错误）。",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "通知标题，必填"},
            "body": {"type": "string", "description": "通知正文，可空"},
            "channels": {
                "type": "string",
                "description": "目标渠道，逗号分隔（如 'inapp,serverchan'）；可选 inapp/serverchan/feishu/feishu_app，或 group:组名 引用自定义通知组；省略用默认渠道",
            },
            "image_id": {"type": "integer", "description": "可选，要随通知发送的图片 ID"},
        },
        "required": ["title"],
    },
)
def send_notification(title: str, body: str = "", channels: str | list | None = None,
                      image_id: int | None = None):
    title = str(title or "").strip()
    if not title:
        raise ValueError("title 不能为空")
    chan_list = _parse_channels(channels)

    image_bytes = None
    if image_id is not None:
        from pathlib import Path

        from flask import current_app

        from app.services import image_service

        asset = image_service.get_image(image_id, current_user.id)
        if asset is None:
            raise ValueError(f"图片 {image_id} 不存在或已删除")
        p = Path(current_app.config["IMAGE_DIR"]) / asset.file_path
        if not p.exists():
            raise ValueError(f"图片 {image_id} 的文件不存在")
        image_bytes = p.read_bytes()

    records = notify(title, str(body or ""), chan_list, image_bytes=image_bytes)
    ok = [r for r in records if r.status == "sent"]
    fail = [r for r in records if r.status == "failed"]
    return {
        "summary": f"共 {len(records)} 条，成功 {len(ok)} 条，失败 {len(fail)} 条",
        "records": [
            {"channel": r.channel, "status": r.status, "error": r.error}
            for r in records
        ],
    }


@register_tool(
    "list_notifications",
    "查询最近的通知记录。limit 为返回条数（默认 20，最大 50）。"
    "返回每条通知的 id/channel/title/status/created_at："
    "channel 为渠道（inapp/serverchan/feishu/feishu_app），status 为状态（sent 已发送/failed 失败/pending 待发送），"
    "created_at 为用户本地时区时间字符串。",
    {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "返回条数，默认 20，最大 50"},
        },
        "required": [],
    },
)
def list_notifications(limit: int = 20):
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError("limit 必须是数字") from None
    limit = max(1, min(limit, 50))
    tz = _tz()
    rows = Notification.query.order_by(Notification.created_at.desc()).limit(limit).all()
    return [
        {
            "id": n.id,
            "channel": n.channel,
            "title": n.title,
            "status": n.status,
            "created_at": fmt_dt(n.created_at, tz),
        }
        for n in rows
    ]
