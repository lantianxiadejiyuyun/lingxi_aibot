"""图片资源模型：AI 生成 / 编辑的图片。

- file_path：相对 data/images 的文件名（uuid 化，不可枚举）
- is_public=False 仅登录用户可见；True 任何人可访问（供生成的网页嵌入）
- parent_id：AI 改图产生的新图关联原图（修改链路）
- 软删除：deleted_at 非空视为已删除（磁盘文件保留，可回滚）
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.utils.timeutil import utcnow


class ImageAsset(db.Model):
    __tablename__ = "image_assets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")       # 生成提示词
    instruction: Mapped[str] = mapped_column(Text, nullable=False, default="")  # 改图要求（编辑/降级重生成）
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    size: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    file_path: Mapped[str] = mapped_column(String(255), nullable=False, default="", index=True)
    parent_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)    # 改图来源图片 id
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # 软删除
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    def __repr__(self) -> str:
        return f"<ImageAsset {self.id} {self.file_path}>"
