"""长期记忆模型：跨对话抽取的事实/偏好，注入每次对话上下文。

- importance: 重要度 1-5（抽取时由 LLM 打分；手动记录可指定）
- expires_at: 过期时间（naive UTC，可空）；过期后自动降级/遗忘（不再注入，梳理时软删）
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.utils.timeutil import utcnow

SOURCE_AUTO = "auto"      # 定时梳理自动抽取（下次梳理时整体替换）
SOURCE_MANUAL = "manual"  # 用户/AI 手动记录


class Memory(db.Model):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default=SOURCE_AUTO)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=3)  # 1-5
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # 软删除
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:
        return f"<Memory {self.id} {self.source} imp={self.importance}>"
