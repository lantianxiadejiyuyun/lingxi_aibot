from sqlalchemy import JSON, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class Setting(db.Model):
    """键值配置，运行时配置优先读这里（缺省回退 .env）。

    user_id=0 为全局设置（部署级：域名/安全入口等）；user_id>0 为用户级设置
    （LLM Key 只读用户自己的记录、不回退全局；通知渠道、备份/简报等用户级优先、缺省回退全局）。
    """

    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("key", "user_id", name="uq_setting_key_user"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    value = mapped_column(JSON, nullable=False, default=None)

    def __repr__(self) -> str:
        return f"<Setting {self.key} user={self.user_id}>"
