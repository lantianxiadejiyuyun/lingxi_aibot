"""Account-owned QQ routes, one-time binding challenges and inbound deduplication."""
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.utils.timeutil import utcnow


class OneBotBinding(db.Model):
    __tablename__ = "onebot_bindings"
    __table_args__ = (UniqueConstraint("self_id", "qq_user_id", name="uq_onebot_private_route"),)

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    self_id: Mapped[str] = mapped_column(String(20), nullable=False)
    qq_user_id: Mapped[str] = mapped_column(String(20), nullable=False)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    generation: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class OneBotBindingCode(db.Model):
    __tablename__ = "onebot_binding_codes"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class OneBotEvent(db.Model):
    __tablename__ = "onebot_events"
    __table_args__ = (Index("ix_onebot_events_created_at", "created_at"),)

    self_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="processing", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
