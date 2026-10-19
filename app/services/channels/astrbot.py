"""AstrBot pulls account-owned notifications and acknowledges platform delivery."""
from app.services.notify_service import BaseChannel, register_channel
from app.utils.scoping import current_user_id


@register_channel
class AstrBotChannel(BaseChannel):
    name = "astrbot"
    display_name = "AstrBot 私聊"
    queued = True

    @property
    def configured(self):
        from app.services.astrbot_service import binding_view
        uid = current_user_id()
        return bool(uid and binding_view(uid)["bound"])

    def send(self, title, body, image_bytes=None):
        from app.services.astrbot_service import enqueue
        if image_bytes:
            raise ValueError("AstrBot 桥接当前支持文本，请使用飞书原生渠道发送图片")
        enqueue(current_user_id(), title, body)

    def deliver(self, notification, image_bytes=None, delivery_context=None):
        from app.services.astrbot_service import enqueue
        uid = current_user_id()
        if not uid or notification.user_id != uid:
            raise ValueError("AstrBot 投递必须绑定通知所属账号")
        if image_bytes:
            raise ValueError("AstrBot 桥接当前支持文本，请使用飞书原生渠道发送图片")
        kind = "companion" if (delivery_context or {}).get("kind") == "companion" else "notification"
        enqueue(uid, notification.title, notification.body, notification.id, kind=kind)
