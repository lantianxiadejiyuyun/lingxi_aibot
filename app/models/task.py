from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.utils.timeutil import utcnow

STATUS_OPEN = "open"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
TASK_STATUSES = (STATUS_OPEN, STATUS_DONE, STATUS_CANCELLED)

PRIORITY_LOW = 1
PRIORITY_MEDIUM = 2
PRIORITY_HIGH = 3
TASK_PRIORITIES = (PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH)


class Task(db.Model):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    due_utc: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=PRIORITY_MEDIUM)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_OPEN, index=True)
    project: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # 软删除
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    @property
    def is_open(self) -> bool:
        return self.status == STATUS_OPEN

    @property
    def is_done(self) -> bool:
        return self.status == STATUS_DONE

    def __repr__(self) -> str:
        return f"<Task {self.id} {self.title}>"
