"""简报计划设置：复用内置任务的开关、时间和通知渠道。"""
from __future__ import annotations

import re

from flask import current_app

from app.extensions import db
from app.models.scheduled_job import ScheduledJob
from app.models.setting import Setting
from app.services.notify_service import (
    CHANNELS, GROUP_PREFIX, default_channels, expand_channels,
    list_notify_groups, scene_channels,
)
from app.services.settings_service import get_setting_from
from app.utils.timeutil import fmt_dt, user_tz

BRIEFINGS = {
    "morning": ("morning_briefing", "早安简报", "07:00", "整理今日日程、待办与优先事项。"),
    "noon": ("noon_briefing", "午间简报", "12:00", "查看今日进展、剩余安排与明日计划。"),
    "evening": ("evening_review", "晚间复盘", "21:00", "回顾完成事项，梳理未完成任务与明日安排。"),
}


def _time(value: str) -> str:
    if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", value):
        raise ValueError("简报时间格式不正确，请使用 HH:MM")
    hour, minute = map(int, value.split(":"))
    return f"{hour:02d}:{minute:02d}"


def _channels(params: dict) -> list[str]:
    raw = params.get("channels") or params.get("channel") or []
    if isinstance(raw, str):
        raw = [c.strip() for c in raw.split(",") if c.strip()]
    return list(raw) if isinstance(raw, (list, tuple)) else []


def briefing_job(user, kind: str, *, create: bool = False):
    action, label, time, _ = BRIEFINGS[kind]
    job = ScheduledJob.query.filter_by(user_id=user.id, job_key=action).first()
    if job is None and create:
        time = _time(str(get_setting_from(f"briefing_time_{kind}", None, time, user_id=user.id)))
        hour, minute = map(int, time.split(":"))
        job = ScheduledJob(user_id=user.id, job_key=action, action=action, name=label,
                           cron=f"{minute} {hour} * * *", params={},
                           enabled=True, is_builtin=True)
        db.session.add(job)
    return job


def briefing_view(user) -> dict:
    rows = []
    inherited = scene_channels("briefing") or default_channels()
    for kind, (_, label, time, description) in BRIEFINGS.items():
        job = briefing_job(user, kind)
        selected = _channels(job.params or {}) if job else []
        time = get_setting_from(f"briefing_time_{kind}", None, time, user_id=user.id)
        if job:
            parts = job.cron.split()
            if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit():
                time = f"{int(parts[1]):02d}:{int(parts[0]):02d}"
        effective = expand_channels(selected or inherited) or ["inapp"]
        rows.append({
            "kind": kind, "label": label, "description": description, "time": time,
            "enabled": job.enabled if job else True, "channels": selected,
            "effective_labels": [CHANNELS[c].display_name for c in effective],
            "last_status": job.last_status if job else "",
            "last_run_text": fmt_dt(job.last_run_utc, user_tz(user)) if job else "",
        })
    return {"rows": rows, "timezone": current_app.config.get("SCHEDULER_TIMEZONE") or "Asia/Shanghai"}


def save_briefing_settings(user, form) -> None:
    """先校验三组输入，再在同一事务更新；旧时间表单保留开关与渠道。"""
    delivery = form.get("briefing_delivery_settings") == "1"
    valid_channels = set(CHANNELS) | {f"{GROUP_PREFIX}{name}" for name in list_notify_groups()}
    updates = {}
    for kind in BRIEFINGS:
        time = _time((form.get(f"briefing_time_{kind}") or "").strip())
        channels = list(dict.fromkeys(form.getlist(f"briefing_channels_{kind}")))
        if delivery and any(c not in valid_channels for c in channels):
            raise ValueError("推送渠道无效，请重新选择渠道或通知组")
        updates[kind] = (time, channels)
    for kind, (time, channels) in updates.items():
        job = briefing_job(user, kind, create=True)
        hour, minute = map(int, time.split(":"))
        job.cron = f"{minute} {hour} * * *"
        if delivery:
            job.enabled = form.get(f"briefing_enabled_{kind}") == "1"
            params = dict(job.params or {})
            params.pop("channel", None)  # 清除旧版本固定站内的覆盖
            params.pop("channels", None)
            if channels:
                params["channels"] = channels
            job.params = params
        key = f"briefing_time_{kind}"
        setting = Setting.query.filter_by(user_id=user.id, key=key).first()
        if setting is None:
            db.session.add(Setting(user_id=user.id, key=key, value=time))
        else:
            setting.value = time
    db.session.commit()
    from app.services.job_service import reschedule

    reschedule()
