from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class Setting(db.Model):
    """键值配置，运行时配置优先读这里（缺省回退 .env）。"""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value = mapped_column(JSON, nullable=False, default=None)

    def __repr__(self) -> str:
        return f"<Setting {self.key}>"
