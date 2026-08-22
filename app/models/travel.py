from datetime import date, datetime
from typing import Optional

from sqlalchemy import JSON, Date, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.utils.timeutil import utcnow


class TripPlan(db.Model):
    """出行计划：目的地 / 日期 / 多段交通 / 备选方案 / 预算 / 同行人 / 备注 / AI 行程。

    - transports: 涉及的交通方式（多选，list[str]），如 ["飞机", "打车", "火车"]
    - segments:   多段交通（list[dict]），每段 {"type","from","to","time","note"}
    - alternatives: 备选方案（list[dict]），每项 {"title","desc"}
    """

    __tablename__ = "trip_plans"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    destination: Mapped[str] = mapped_column(String(128), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    transports: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    segments: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    alternatives: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    budget: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 预算（元）
    companions: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    itinerary: Mapped[str] = mapped_column(Text, nullable=False, default="")  # AI 生成的行程（Markdown）
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:
        return f"<TripPlan {self.id} {self.destination}>"
