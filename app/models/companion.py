"""主动陪伴的租约与可审计活动；只保存事实和对外消息，不保存模型思维链。"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.utils.timeutil import utcnow


class CompanionState(db.Model):
    __tablename__ = "companion_states"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lease_token: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Accepted deliveries reserve the interval, including asynchronous channels.
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CompanionActivity(db.Model):
    __tablename__ = "companion_activities"
    __table_args__ = (UniqueConstraint("user_id", "dedupe_key", name="uq_companion_user_dedupe"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sources: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    notification_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, index=True)
