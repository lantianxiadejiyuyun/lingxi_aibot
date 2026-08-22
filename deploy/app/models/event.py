from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.utils.timeutil import utcnow


class Event(db.Model):
    """日历事件。时间均为 naive UTC；重复事件存 rrule 字符串，删除为软删除。"""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    start_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    end_utc: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rrule: Mapped[str] = mapped_column(String(255), nullable=False, default="")  # RFC5545，空=不重复
    reminder_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # None=不提醒
    location: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # 软删除
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:
        return f"<Event {self.id} {self.title}>"
