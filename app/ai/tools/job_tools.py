"""AI 工具：定时任务管理（查询 / 启停 / 改计划 / 新建提醒 / 删除）。

工具内时间字段（last_run）用 fmt_dt 转好的用户时区字符串返回给模型；
cron 为 5 段表达式（分 时 日 月 周），也支持 'interval:N'（每 N 分钟）。
"""
from __future__ import annotations

from flask_login import current_user

from app.ai.registry import register_tool
from app.extensions import db
from app.models.scheduled_job import ScheduledJob
from app.services import job_service
from app.utils.timeutil import fmt_dt, user_tz


def _find_job(job_id) -> ScheduledJob:
    """按 id 取任务，非法或不存在抛 ValueError（信息回给模型自纠）。"""
    try:
        jid = int(job_id)
    except (TypeError, ValueError):
        raise ValueError("job_id 必须是数字") from None
    job = db.session.get(ScheduledJob, jid)
    if job is None or job.user_id != current_user.id:
        raise ValueError(f"任务 {jid} 不存在")
    return job


@register_tool(
    "list_scheduled_jobs",
    "列出全部定时任务（内置与自定义都列出）。返回任务列表，每项含："
    "id（任务 ID）、name（任务名称）、action（动作标识）、cron（计划表达式）、"
    "enabled（是否启用）、last_run（上次执行时间，用户本地时区字符串，未执行为空字符串）、"
    "last_status（上次执行结果，'成功' 或 '失败: ...' 或空）。",
    {
        "type": "object",
        "properties": {},
        "required": [],
    },
)
def list_scheduled_jobs():
    tz = user_tz(current_user)
    out = []
    for job in job_service.list_jobs(current_user.id):
        out.append({
            "id": job.id,
            "name": job.name,
            "action": job.action,
            "cron": job.cron,
            "enabled": job.enabled,
            "last_run": fmt_dt(job.last_run_utc, tz),
            "last_status": job.last_status or "",
        })
    return out


@register_tool(
    "set_job_enabled",
    "启用或停用定时任务。job_id 为任务 ID（必填）；enabled 为布尔值："
    "true 启用该任务，false 停用该任务（停用后不再按计划执行）。"
    "返回更新后的任务信息（id/name/enabled）。",
    {
        "type": "object",
        "properties": {
            "job_id": {"type": "integer", "description": "任务 ID，必填"},
            "enabled": {"type": "boolean", "description": "true 启用，false 停用，必填"},
        },
        "required": ["job_id", "enabled"],
    },
)
def set_job_enabled(job_id: int, enabled: bool):
    job = _find_job(job_id)
    job_service.update_job(job, enabled=enabled)
    return {"id": job.id, "name": job.name, "enabled": job.enabled}


@register_tool(
    "update_job_schedule",
    "修改定时任务的 cron 计划。job_id 为任务 ID（必填）；cron 为 5 段 cron 表达式"
    "（分 时 日 月 周），例如每天 9:30 写作 '30 9 * * *'；也可用 'interval:N' 表示每 N 分钟。"
    "返回更新确认信息，非法 cron 会报错。",
    {
        "type": "object",
        "properties": {
            "job_id": {"type": "integer", "description": "任务 ID，必填"},
            "cron": {
                "type": "string",
                "description": "新的计划表达式，5 段 cron（如 '30 9 * * *'）或 'interval:N'，必填",
            },
        },
        "required": ["job_id", "cron"],
    },
)
def update_job_schedule(job_id: int, cron: str):
    job = _find_job(job_id)
    job_service.update_job(job, cron=cron)
    return f"已更新任务「{job.name}」计划：{job.cron}"


@register_tool(
    "create_reminder_job",
    "创建自定义定时提醒任务。name 为任务名称（必填，用于列表展示）；cron 为 5 段 cron 表达式"
    "（分 时 日 月 周），例如每天 9:30 写作 '30 9 * * *'（必填）；title 为提醒通知标题（必填）；"
    "body 为提醒通知正文（可空）；channels 为通知渠道（可空，默认站内；可选值 inapp/serverchan/"
    "feishu/feishu_app，多个渠道用逗号分隔；也可填「group:组名」引用设置页自定义通知组）。"
    "返回创建确认信息。",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "任务名称，必填"},
            "cron": {
                "type": "string",
                "description": "5 段 cron 表达式（分 时 日 月 周），如每天 9:30 = '30 9 * * *'，必填",
            },
            "title": {"type": "string", "description": "提醒通知标题，必填"},
            "body": {"type": "string", "description": "提醒通知正文，可空"},
            "channels": {
                "type": "string",
                "description": "通知渠道，逗号分隔（inapp/serverchan/feishu/feishu_app），或 group:组名；可空，默认站内",
            },
        },
        "required": ["name", "cron", "title"],
    },
)
def create_reminder_job(name: str, cron: str, title: str, body: str = "",
                        channels: str | None = None):
    channel_list = None
    if channels:
        from app.services.notify_service import (
            CHANNELS, GROUP_PREFIX, list_notify_groups,
        )

        items = [c.strip() for c in channels.split(",") if c.strip()]
        groups = set(list_notify_groups())
        bad = [
            c for c in items
            if c not in CHANNELS
            and not (c.startswith(GROUP_PREFIX) and c[len(GROUP_PREFIX):] in groups)
        ]
        if bad:
            valid = sorted(CHANNELS) + [f"{GROUP_PREFIX}{n}" for n in sorted(groups)]
            raise ValueError(f"未知渠道或通知组: {', '.join(bad)}（可选: {', '.join(valid)}）")
        channel_list = items
    job = job_service.create_reminder_job(
        current_user.id, name=name, cron=cron, title=title, body=body, channels=channel_list,
    )
    return (f"已创建定时提醒「{job.name}」（{job.cron}）。"
            f"cron 格式为 5 段：分 时 日 月 周，例如每天 9:30 = 30 9 * * *")


@register_tool(
    "delete_scheduled_job",
    "删除定时任务（仅自定义任务可删，内置任务会报错）。job_id 为任务 ID（必填）。"
    "此操作不可恢复，请在确认用户明确要求删除后再调用。",
    {
        "type": "object",
        "properties": {"job_id": {"type": "integer", "description": "任务 ID，必填"}},
        "required": ["job_id"],
    },
    dangerous=True,
)
def delete_scheduled_job(job_id: int):
    job = _find_job(job_id)
    name = job.name
    job_service.delete_job(job)  # 内置任务会抛 ValueError
    return f"🗑️ 已删除定时任务「{name}」"
