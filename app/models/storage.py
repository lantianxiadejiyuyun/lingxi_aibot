"""User-owned storage roots used by the file service and download workers."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.utils.timeutil import utcnow


class StorageLocation(db.Model):
    __tablename__ = "storage_locations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="local")
    root_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    download_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    marker_name: Mapped[str] = mapped_column(String(96), nullable=False)
    marker_token: Mapped[str] = mapped_column(String(64), nullable=False)
    root_device: Mapped[str] = mapped_column(String(64), nullable=False)
    min_free_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=268435456)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    def to_dict(self, *, include_paths: bool = False) -> dict:
        result = {
            "id": self.id, "user_id": self.user_id, "name": self.name, "kind": self.kind,
            "enabled": self.enabled, "min_free_bytes": self.min_free_bytes,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
        }
        if include_paths:
            result.update(root_path=self.root_path, download_path=self.download_path)
        return result
