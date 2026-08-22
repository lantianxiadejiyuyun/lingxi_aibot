from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.utils.timeutil import utcnow

# 内置任务 action 常量
ACTION_EVENT_REMINDER = "event_reminder_scan"   # 每分钟
ACTION_TASK_DUE = "task_due_scan"               # 每 10 分钟
ACTION_MORNING_BRIEFING = "morning_briefing"    # cron 0 7 * * *
ACTION_NOON_BRIEFING = "noon_briefing"          # cron 0 12 * * *
ACTION_EVENING_REVIEW = "evening_review"        # cron 0 21 * * *
ACTION_DATA_BACKUP = "data_backup"              # cron 0 3 * * *
ACTION_CONTEXT_CONSOLIDATION = "context_consolidation"  # 上下文梳理 cron 0 4 * * *
ACTION_WEEKLY_REPORT = "weekly_report"      # 周报 cron 0 18 * * 0（周日 18:00）
ACTION_MONTHLY_REPORT = "monthly_report"    # 月报 cron 0 9 1 * *（每月 1 日 09:00）
ACTION_WEEKLY_CLEANUP = "weekly_cleanup"    # 每周整理 cron 0 2 * * 0（周日 02:00）
ACTION_CUSTOM_REMINDER = "custom_reminder"      # 用户自定义定时提醒
ACTION_FITNESS_REMINDER = "fitness_reminder"    # 每日健身提醒
ACTION_FITNESS_WEEKLY_ANALYSIS = "fitness_weekly_analysis"  # 每周健身分析
ACTION_TRIP_REMINDER = "trip_reminder"          # 出行前一天提醒


class ScheduledJob(db.Model):
    """定时任务定义。

    cron 字段格式：
      - "interval:N" 表示每 N 分钟执行一次
      - 标准 5 段 cron（时区取应用时区），如 "0 7 * * *"
    """

    __tablename__ = "scheduled_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    job_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    cron: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # 内置任务不可删除
    last_run_utc: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    def __repr__(self) -> str:
        return f"<ScheduledJob {self.job_key}>"
