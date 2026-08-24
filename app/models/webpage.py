"""网页模型：AI 生成 / 手动创建的自由 HTML 页面。

- slug 唯一标识访问路径（/webs/html/<slug>）
- is_public=False 时仅登录用户可访问
- enabled=False 时后台隐藏（前台 404），即"显示开关"
- 软删除：deleted_at 非空视为已删除
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.utils.timeutil import utcnow


class WebPage(db.Model):
    __tablename__ = "webpages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")  # 完整 HTML 源码
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)  # 显示开关
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # 软删除
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:
        return f"<WebPage {self.id} {self.slug}>"
