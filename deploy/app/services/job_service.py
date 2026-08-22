"""定时任务服务：内置/自定义任务的增删改查 + 自定义提醒调度动作。

- 内置任务（is_builtin=True）由 init-db 种子创建，不可删除；
- 自定义任务 action 固定为 custom_reminder，params 存 {title, body, channels}；
- 每次增删改后调用 reschedule() 让 APScheduler 重新同步。
"""
from __future__ import annotations

from uuid import uuid4

from flask import current_app

from app.extensions import db
from app.models.scheduled_job import ACTION_CUSTOM_REMINDER, ScheduledJob
from app.scheduler import register_action, trigger_from_cron


def _validate_cron(cron: str) -> None:
    """用 trigger_from_cron 校验 cron（'interval:N' 或 5 段 cron），非法抛 ValueError。"""
    try:
        trigger_from_cron(cron, current_app.config["SCHEDULER_TIMEZONE"])
    except Exception:  # noqa: BLE001 —— 统一转为业务错误信息
        raise ValueError("cron 格式不合法") from None


def list_jobs() -> list[ScheduledJob]:
    """全部任务，内置的排前面。"""
    return ScheduledJob.query.order_by(
        ScheduledJob.is_builtin.desc(), ScheduledJob.id.asc()
    ).all()


def create_reminder_job(name: str, cron: str, title: str, body: str,
                        channels: list[str] | None = None) -> ScheduledJob:
    """创建自定义定时提醒任务。cron 非法抛 ValueError。"""
    _validate_cron(cron)
    job = ScheduledJob(
        job_key=f"custom_{uuid4().hex[:8]}",
        name=name,
        action=ACTION_CUSTOM_REMINDER,
        cron=cron,
        enabled=True,
        params={"title": title, "body": body, "channels": channels or ["inapp"]},
        is_builtin=False,
    )
    db.session.add(job)
    db.session.commit()
    reschedule()
    return job


def update_job(job: ScheduledJob, cron: str | None = None, enabled: bool | None = None,
               name: str | None = None, params: dict | None = None) -> ScheduledJob:
    """更新任务字段；改 cron 时同样校验，非法抛 ValueError。"""
    if cron is not None:
        _validate_cron(cron)
        job.cron = cron
    if enabled is not None:
        job.enabled = bool(enabled)
    if name is not None:
        job.name = name
    if params is not None:
        job.params = params
    db.session.commit()
    reschedule()
    return job


def delete_job(job: ScheduledJob) -> None:
    """删除任务；内置任务抛 ValueError。"""
    if job.is_builtin:
        raise ValueError("内置任务不可删除")
    db.session.delete(job)
    db.session.commit()
    reschedule()


def reschedule() -> None:
    """调度器可用时重新同步任务。"""
    if current_app.scheduler:
        current_app.scheduler.reschedule()


@register_action("custom_reminder", description="用户自定义定时提醒（参数：title/body/channels）")
def run_custom_reminder(params: dict | None = None) -> None:
    """调度动作：按 params 发送站内/渠道通知。"""
    from app.services.notify_service import notify

    params = params or {}
    notify(
        params.get("title", "定时提醒"),
        params.get("body", ""),
        params.get("channels") or None,
    )
