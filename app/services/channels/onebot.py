"""Synchronous private QQ notification delivery through the direct OneBot link."""
from app.services.notify_service import BaseChannel, register_channel
from app.utils.scoping import current_user_id


@register_channel
class OneBotChannel(BaseChannel):
    name = "onebot"
    display_name = "QQ 个人号（NapCat）"

    @property
    def configured(self):
        from app.services.onebot_service import binding_view
        uid = current_user_id()
        return bool(uid and binding_view(uid)["bound"])

    def send(self, title, body, image_bytes=None):
        from app.services.onebot_service import send_notification
        send_notification(current_user_id(), title, body, image_bytes=image_bytes)

    def deliver(self, notification, image_bytes=None, delivery_context=None):
        uid = current_user_id()
        if not uid or notification.user_id != uid:
            raise ValueError("QQ 投递必须绑定通知所属账号")
        self.send(notification.title, notification.body, image_bytes=image_bytes)
