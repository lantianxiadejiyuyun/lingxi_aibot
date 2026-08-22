from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.utils.timeutil import utcnow


class ExpenseRecord(db.Model):
    """消费记录：金额 / 分类 / 日期 / 支付方式 / 备注。"""

    __tablename__ = "expense_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)  # 金额（元）
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="其他")
    date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_method: Mapped[str] = mapped_column(String(32), nullable=False, default="其他")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:
        return f"<ExpenseRecord {self.id} {self.date} {self.amount}>"
