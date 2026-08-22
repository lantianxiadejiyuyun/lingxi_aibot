from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.utils.timeutil import utcnow

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"

# 渠道名常量
CHANNEL_INAPP = "inapp"
CHANNEL_SERVERCHAN = "serverchan"
CHANNEL_FEISHU = "feishu"


class Notification(db.Model):
    """通知记录：一次 notify 调用可能产生多条（每渠道一条）。"""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_PENDING, index=True)
    error: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # 站内已读标记
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<Notification {self.id} {self.channel}>"
