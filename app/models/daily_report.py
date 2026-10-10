"""One durable briefing archive per account and local calendar date."""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import DATETIME, LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.utils.timeutil import utcnow


class DailyReport(db.Model):
    __tablename__ = "daily_reports"
    __table_args__ = (UniqueConstraint("user_id", "report_date", name="uq_daily_report_user_date"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    morning_content: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"), default="", nullable=False)
    noon_content: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"), default="", nullable=False)
    evening_content: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"), default="", nullable=False)
    # References are optional: deleting chat history must preserve the archive.
    morning_conversation_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    noon_conversation_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    evening_conversation_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    morning_generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=True)
    noon_generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=True)
    evening_generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
