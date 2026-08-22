"""定时任务管理页：内置任务 / 自定义定时提醒的查看、编辑、立即执行、删除。"""
from __future__ import annotations

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models.scheduled_job import ScheduledJob
from app.services import job_service
from app.utils.timeutil import fmt_dt, user_tz

bp = Blueprint("jobs", __name__, url_prefix="/jobs")

# 内置动作 → 中文说明（页面展示用）
_ACTION_LABELS = {
    "event_reminder_scan": "事件提醒扫描",
    "task_due_scan": "任务到期扫描",
    "morning_briefing": "早安简报",
    "noon_briefing": "午间简报",
    "evening_review": "晚间复盘",
    "data_backup": "数据备份",
    "context_consolidation": "上下文梳理",
    "weekly_report": "周报",
    "monthly_report": "月报",
    "weekly_cleanup": "每周整理",
    "custom_reminder": "自定义提醒",
}

_CHANNEL_OPTIONS = [
    ("", "默认渠道"),
    ("inapp", "站内"),
    ("serverchan", "Server酱"),
    ("feishu", "飞书"),
    ("feishu_app", "飞书机器人(应用)"),
]


def _job_row(job: ScheduledJob, tz) -> dict:
    """ScheduledJob → 模板渲染用字典（时间已转用户时区字符串）。"""
    cron = job.cron or ""
    is_interval = cron.startswith("interval:")
    parts = cron.split()
    last = (job.last_status or "").strip()
    params = job.params or {}
    channels = params.get("channels") or []
    channel = channels[0] if channels else (params.get("channel") or "")
    return {
        "id": job.id,
        "name": job.name,
        "action": job.action,
        "action_label": _ACTION_LABELS.get(job.action, job.action),
        "cron": cron,
        "is_interval": is_interval,
        "interval_min": cron.split(":", 1)[1] if is_interval else "",
        "cron_min": parts[0] if len(parts) > 0 and not is_interval else "",
        "cron_hour": parts[1] if len(parts) > 1 and not is_interval else "",
        "enabled": job.enabled,
        "last_run_text": fmt_dt(job.last_run_utc, tz),
        "last_status": last,
        "status_ok": last.startswith("成功"),
        "status_fail": last.startswith("失败"),
        "status_other": bool(last) and not last.startswith("成功") and not last.startswith("失败"),
        "title": params.get("title", ""),
        "body": params.get("body", ""),
        "channel": channel,
    }


@bp.route("/")
@login_required
def index():
    tz = user_tz(current_user)
    jobs = job_service.list_jobs()
    builtin = [_job_row(j, tz) for j in jobs if j.is_builtin]
    custom = [_job_row(j, tz) for j in jobs if not j.is_builtin]
    return render_template(
        "jobs/index.html",
        builtin=builtin,
        custom=custom,
        channel_options=_CHANNEL_OPTIONS,
        tz_name=str(tz),
    )


@bp.route("/update/<int:job_id>", methods=["POST"])
@login_required
def update(job_id: int):
    job = db.session.get(ScheduledJob, job_id)
    if job is None:
        flash("任务不存在", "error")
        return redirect(url_for("jobs.index"))

    kwargs = {"enabled": request.form.get("enabled") == "1"}

    if job.is_builtin:
        # 内置任务：interval 型编辑分钟数，cron 型编辑 分/时；可指定推送渠道（空=默认渠道）
        if request.form.get("cron_type") == "interval":
            minutes = (request.form.get("cron_interval") or "").strip()
            if not minutes:
                flash("请输入间隔分钟数", "error")
                return redirect(url_for("jobs.index"))
            kwargs["cron"] = f"interval:{minutes}"
        else:
            minute = (request.form.get("cron_min") or "").strip()
            hour = (request.form.get("cron_hour") or "").strip()
            if minute == "" or hour == "":
                flash("请输入分与时", "error")
                return redirect(url_for("jobs.index"))
            kwargs["cron"] = f"{minute} {hour} * * *"
        ch_raw = (request.form.get("channels") or "").strip()
        if ch_raw:
            params = dict(job.params or {})
            params["channels"] = [ch_raw]
            kwargs["params"] = params
    else:
        cron = (request.form.get("cron") or "").strip()
        if not cron:
            flash("cron 不能为空", "error")
            return redirect(url_for("jobs.index"))
        kwargs["cron"] = cron
        name = (request.form.get("name") or "").strip()
        if name:
            kwargs["name"] = name
        ch_raw = (request.form.get("channels") or "").strip()
        kwargs["params"] = {
            "title": (request.form.get("title") or "").strip() or "定时提醒",
            "body": request.form.get("body") or "",
            "channels": [ch_raw] if ch_raw else ["inapp"],
        }

    try:
        job_service.update_job(job, **kwargs)
        flash("已保存", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("jobs.index"))


@bp.route("/run/<int:job_id>", methods=["POST"])
@login_required
def run(job_id: int):
    sched = getattr(current_app, "scheduler", None)
    if sched is None:
        flash("调度器未启用，无法立即执行", "error")
        return redirect(url_for("jobs.index"))
    try:
        sched.run_now(job_id)
        flash("执行成功", "success")
    except Exception as e:  # noqa: BLE001 —— 立即执行失败信息反馈给用户
        flash(f"执行失败：{e}", "error")
    return redirect(url_for("jobs.index"))


@bp.route("/delete/<int:job_id>", methods=["POST"])
@login_required
def delete(job_id: int):
    job = db.session.get(ScheduledJob, job_id)
    if job is None:
        flash("任务不存在", "error")
        return redirect(url_for("jobs.index"))
    try:
        job_service.delete_job(job)
        flash("已删除", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("jobs.index"))


@bp.route("/create", methods=["POST"])
@login_required
def create():
    name = (request.form.get("name") or "").strip()
    cron = (request.form.get("cron") or "").strip()
    title = (request.form.get("title") or "").strip()
    body = request.form.get("body") or ""
    ch_raw = (request.form.get("channels") or "").strip()
    if not name:
        flash("名称不能为空", "error")
        return redirect(url_for("jobs.index"))
    if not cron:
        flash("cron 不能为空", "error")
        return redirect(url_for("jobs.index"))
    if not title:
        flash("提醒标题不能为空", "error")
        return redirect(url_for("jobs.index"))
    try:
        job = job_service.create_reminder_job(
            name=name, cron=cron, title=title, body=body,
            channels=[ch_raw] if ch_raw else None,
        )
        flash(f"已创建定时提醒「{job.name}」", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("jobs.index"))
