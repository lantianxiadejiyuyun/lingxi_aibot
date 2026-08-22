"""向量索引模型：RAG 语义检索的嵌入向量缓存（每来源一条）。"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.utils.timeutil import utcnow


class Embedding(db.Model):
    __tablename__ = "embeddings"
    __table_args__ = (db.Index("ix_embeddings_source", "source_type", "source_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)   # note/webpage/conversation
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[list] = mapped_column(JSON, nullable=False)             # 归一化嵌入向量
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:
        return f"<Embedding {self.source_type}:{self.source_id}>"
