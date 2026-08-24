"""调度框架：APScheduler 封装 + 动作注册表。

- 动作通过 @register_action 注册（各服务模块注册自己的动作）
- 任务定义存在 ScheduledJob 表，启动时同步到 APScheduler
- 每次任务编辑后调用 reschedule() 重新同步
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.extensions import db
from app.models.scheduled_job import ScheduledJob
from app.models.user import User
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)

ACTIONS: dict[str, Callable] = {}
ACTION_META: dict[str, dict] = {}  # 供 UI 展示动作说明


def register_action(name: str, description: str = "", editable: bool = True):
    """注册调度动作。动作函数签名：fn(params: dict | None) -> None（异常会被记录）。"""

    def deco(fn: Callable) -> Callable:
        ACTIONS[name] = fn
        ACTION_META[name] = {"description": description, "editable": editable}
        return fn

    return deco


def trigger_from_cron(cron: str, tz_name: str):
    """'interval:N' 或 5 段 cron → APScheduler trigger。"""
    cron = (cron or "").strip()
    if cron.startswith("interval:"):
        minutes = max(1, int(cron.split(":", 1)[1]))
        return IntervalTrigger(minutes=minutes)
    return CronTrigger.from_crontab(cron, timezone=tz_name)


def _job_func(db_id: int, app):
    """APScheduler 包装：在应用上下文中执行动作并记录结果。动作签名 fn(user, params)。"""
    with app.app_context():
        row = db.session.get(ScheduledJob, db_id)
        if row is None or not row.enabled:
            return
        user = db.session.get(User, row.user_id) if row.user_id else None
        if user is None:
            row.last_status = "用户不存在"
            db.session.commit()
            return
        fn = ACTIONS.get(row.action)
        if fn is None:
            row.last_status = f"未注册的动作: {row.action}"
            db.session.commit()
            return
        row.last_run_utc = utcnow()
        from app.utils.scoping import set_current_user_id, clear_current_user_id

        set_current_user_id(user.id)
        try:
            fn(user, row.params or {})
            row.last_status = "成功"
            logger.info("任务执行成功: %s", row.job_key)
            db.session.commit()
        except Exception as e:  # noqa: BLE001 —— 任务异常不中断调度器
            logger.exception("任务执行失败: %s", row.job_key)
            db.session.rollback()
            try:
                row = db.session.get(ScheduledJob, db_id)
                if row is not None:
                    row.last_run_utc = utcnow()
                    row.last_status = f"失败: {e}"
                    db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
                logger.exception("任务失败状态无法写入: %s", row.job_key if row else db_id)
        finally:
            clear_current_user_id()


class SchedulerService:
    def __init__(self, app):
        self.app = app
        tz = app.config.get("SCHEDULER_TIMEZONE") or "Asia/Shanghai"
        self.scheduler = BackgroundScheduler(timezone=tz, daemon=True)
        self._last_signature: Optional[str] = None

    # ---------- 生命周期 ----------
    def start(self):
        self.sync()
        self.scheduler.start()
        logger.info("调度器已启动（时区 %s），任务数 %d", self.app.config.get("SCHEDULER_TIMEZONE"),
                    len(self.scheduler.get_jobs()))

    def shutdown(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    # ---------- 同步 ----------
    def _desired_jobs(self) -> dict[str, dict]:
        """DB 中启用的任务 → {aps_job_id: (job_id, trigger)}。

        必须在 app_context 内把字段拷出来：上下文结束会 rollback/expire，不能在外面读 ORM 对象。
        """
        with self.app.app_context():
            snaps = [
                {"id": row.id, "job_key": row.job_key, "cron": row.cron}
                for row in ScheduledJob.query.filter_by(enabled=True).all()
            ]
        desired = {}
        for snap in snaps:
            try:
                trig = trigger_from_cron(snap["cron"], self.app.config["SCHEDULER_TIMEZONE"])
            except Exception as e:
                logger.warning("任务 %s 的 cron 非法（%s）：%s", snap["job_key"], snap["cron"], e)
                continue
            desired[f"sj-{snap['id']}"] = (snap["id"], snap["cron"], trig)
        return desired

    def sync(self):
        from apscheduler.jobstores.base import JobLookupError

        desired = self._desired_jobs()
        existing = {j.id: j for j in self.scheduler.get_jobs()}
        # 删除多余（并发 reschedule 时 job 可能已被移除，忽略）
        for jid in set(existing) - set(desired):
            try:
                self.scheduler.remove_job(jid)
            except JobLookupError:
                pass
            logger.info("移除调度任务 %s", jid)
        # 添加/更新
        for jid, (row_id, _cron, trig) in desired.items():
            kwargs = {
                "id": jid,
                "func": _job_func,
                "args": [row_id, self.app],
                "trigger": trig,
                "replace_existing": True,
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 300,
            }
            if jid in existing:
                # 用 reschedule_job 而非 modify_job：modify_job 改 trigger 时不会重算
                # next_run_time，导致改完时间后仍按旧时间触发（首跑错点/看似不触发）。
                self.scheduler.reschedule_job(jid, trigger=trig)
            else:
                self.scheduler.add_job(**kwargs)
        self._last_signature = self._signature(desired)
        logger.info("调度同步完成，共 %d 个任务", len(desired))

    def reschedule(self):
        """任务增删改后调用。"""
        if self.scheduler.running:
            self.sync()
        else:
            logger.debug("调度器未运行，跳过同步")

    def _signature(self, desired) -> str:
        parts = []
        for jid, (_row_id, cron, trig) in sorted(desired.items()):
            parts.append(f"{jid}:{cron}:{str(trig)}")
        return "|".join(parts)

    # ---------- 手动执行 ----------
    def run_now(self, db_id: int) -> str:
        with self.app.app_context():
            row = db.session.get(ScheduledJob, db_id)
            if row is None:
                raise ValueError("任务不存在")
            user = db.session.get(User, row.user_id) if row.user_id else None
            if user is None:
                raise ValueError("任务所属用户不存在")
            fn = ACTIONS.get(row.action)
            if fn is None:
                raise ValueError(f"未注册的动作: {row.action}")
            row.last_run_utc = utcnow()
            db.session.commit()
            try:
                fn(user, row.params or {})
                row.last_status = "成功（手动）"
            except Exception as e:
                row.last_status = f"失败: {e}"
                db.session.commit()
                raise
            db.session.commit()
            return row.last_status

    def next_run_time(self, db_id: int) -> Optional[datetime]:
        job = self.scheduler.get_job(f"sj-{db_id}")
        return job.next_run_time if job else None
