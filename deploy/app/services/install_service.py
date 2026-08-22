"""安装服务：网页安装向导的核心逻辑。

- probe_db：直接探测 MySQL 连通性（区分"没启动/地址错""账号密码错""库不存在"等）
- save_db_config：把数据库配置写入项目根 .env（供网页向导保存）
- rebuild_engine：热重建 SQLAlchemy 引擎（改配置后无需重启进程）
- initialize_database：建表 + 补列 + 管理员 + 内置任务（幂等，CLI 与网页共用）
- restart_scheduler：数据库就绪后补启动调度器（首次启动连不上库时调度器会启动失败）
"""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote_plus

import pymysql
from dotenv import set_key

from app.extensions import db

logger = logging.getLogger(__name__)

# 项目根目录（app/services/install_service.py → 上三级）
BASE_DIR = Path(__file__).resolve().parent.parent.parent
ENV_PATH = BASE_DIR / ".env"


def _ensure_env_file() -> None:
    """.env 不存在时创建空文件（set_key 需要目标文件存在）。"""
    if not ENV_PATH.exists():
        ENV_PATH.touch()


# ---------- 数据库探测 ----------

def probe_db(host: str, port, user: str, password: str, db_name: str,
             timeout: int = 5) -> tuple[bool, str]:
    """用 PyMySQL 直接探测连通性（不经过 SQLAlchemy，便于区分错误原因）。"""
    try:
        conn = pymysql.connect(
            host=host or "127.0.0.1", port=int(port or 3306), user=user or "",
            password=password or "", database=db_name or "",
            connect_timeout=timeout, charset="utf8mb4")
        conn.close()
        return True, "连接成功"
    except pymysql.err.OperationalError as e:
        code = e.args[0] if e.args else ""
        if code == 1045:
            return False, "账号或密码错误（1045 Access denied）"
        if code == 1044:
            return False, f"账号无权访问数据库 {db_name!r}（1044），请在宝塔/MySQL 中授权"
        if code == 1049:
            return False, f"数据库 {db_name!r} 不存在（1049），请先在宝塔/MySQL 中创建"
        if code == 2003:
            return False, (f"无法连接 {host}:{port}（2003）：MySQL 未启动，"
                           "或地址/端口错误，或 MySQL 未放行该地址访问")
        return False, f"连接失败：{e}"
    except Exception as e:  # noqa: BLE001
        return False, f"连接失败：{e}"


# ---------- 配置保存与热重建 ----------

def save_db_config(host: str, port, user: str, password: str, db_name: str) -> str:
    """把数据库配置写入项目根 .env。返回提示消息。"""
    _ensure_env_file()
    values = {
        "MYSQL_HOST": (host or "127.0.0.1").strip(),
        "MYSQL_PORT": str(int(port or 3306)),
        "MYSQL_USER": (user or "").strip(),
        "MYSQL_PASSWORD": password or "",
        "MYSQL_DB": (db_name or "").strip(),
    }
    for key, value in values.items():
        set_key(ENV_PATH, key, value, quote_mode="always")
    return f"已保存到项目 {ENV_PATH.name}"


def rebuild_engine(host: str, port, user: str, password: str, db_name: str) -> None:
    """热重建 SQLAlchemy 引擎（无需重启进程，下次 DB 访问用新配置）。

    实现：对齐 Flask-SQLAlchemy init_app 的默认引擎构建逻辑，
    替换 _app_engines 缓存中的默认引擎并 dispose 旧引擎。
    """
    from flask import current_app

    app = current_app._get_current_object()
    uri = (f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
           f"@{host or '127.0.0.1'}:{int(port or 3306)}/{db_name or ''}?charset=utf8mb4")
    app.config["SQLALCHEMY_DATABASE_URI"] = uri

    sa = app.extensions["sqlalchemy"]
    basic_options = sa._engine_options.copy()
    basic_options.update(app.config.get("SQLALCHEMY_ENGINE_OPTIONS", {}))
    basic_options["url"] = uri
    sa._apply_driver_defaults(basic_options, app)

    engines = sa._app_engines.setdefault(app, {})
    old = engines.get(None)
    if old is not None:
        old.dispose()
    engines[None] = sa._make_engine(None, basic_options, app)

    from app.extensions import db

    db.session.remove()


def restart_scheduler(app) -> None:
    """数据库就绪后补启动调度器（首次启动因连不上库而失败时）。"""
    if not app.config.get("SCHEDULER_ENABLED"):
        return
    scheduler = getattr(app, "scheduler", None)
    if scheduler is None:
        return
    try:
        if not scheduler.scheduler.running:
            scheduler.start()
            app.logger.info("调度器已补启动")
    except Exception:  # noqa: BLE001
        app.logger.exception("调度器补启动失败")


# ---------- 数据库初始化（CLI 与网页向导共用） ----------

def ensure_schema(messages: list[str] | None = None) -> None:
    """已有库补齐新增列（db.create_all 不会给已存在的表加列）。"""
    from sqlalchemy import inspect, text

    insp = inspect(db.engine)
    if "conversations" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("conversations")}
        if "summary" not in cols:
            db.session.execute(text("ALTER TABLE conversations ADD COLUMN summary TEXT NULL"))
            if messages is not None:
                messages.append("✓ 已为 conversations 表补充 summary 列")
    if "memories" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("memories")}
        if "importance" not in cols:
            db.session.execute(text(
                "ALTER TABLE memories ADD COLUMN importance INTEGER NOT NULL DEFAULT 3"))
            if messages is not None:
                messages.append("✓ 已为 memories 表补充 importance 列")
        if "expires_at" not in cols:
            db.session.execute(text("ALTER TABLE memories ADD COLUMN expires_at DATETIME NULL"))
            if messages is not None:
                messages.append("✓ 已为 memories 表补充 expires_at 列")
    db.session.commit()


def initialize_database(app) -> list[str]:
    """建表 + 补列 + 初始化管理员/内置定时任务（幂等）。返回过程消息列表。"""
    messages: list[str] = []
    with app.app_context():
        db.create_all()
        messages.append("✓ 数据表已创建")
        ensure_schema(messages)

        from app.models.user import User
        from app.models.scheduled_job import (
            ACTION_CONTEXT_CONSOLIDATION, ACTION_DATA_BACKUP, ACTION_EVENT_REMINDER,
            ACTION_EVENING_REVIEW, ACTION_MONTHLY_REPORT, ACTION_MORNING_BRIEFING,
            ACTION_NOON_BRIEFING, ACTION_TASK_DUE, ACTION_WEEKLY_CLEANUP,
            ACTION_WEEKLY_REPORT, ScheduledJob,
        )

        cfg = app.config
        if User.query.count() == 0:
            admin = User(username=cfg["ADMIN_USERNAME"], timezone=cfg["APP_TIMEZONE"])
            admin.set_password(cfg["ADMIN_PASSWORD"])
            db.session.add(admin)
            messages.append(f"✓ 已创建管理员：{cfg['ADMIN_USERNAME']}"
                            "（请登录后立即修改密码）")
        else:
            messages.append("✓ 管理员已存在，跳过")

        defaults = [
            (ACTION_EVENT_REMINDER, "事件提醒扫描", "interval:1", {}),
            (ACTION_TASK_DUE, "任务到期扫描", "interval:10", {}),
            (ACTION_MORNING_BRIEFING, "早安简报", "0 7 * * *", {"channel": "inapp"}),
            (ACTION_NOON_BRIEFING, "午间简报", "0 12 * * *", {"channel": "inapp"}),
            (ACTION_EVENING_REVIEW, "晚间复盘", "0 21 * * *", {"channel": "inapp"}),
            (ACTION_DATA_BACKUP, "数据备份", "0 3 * * *", {}),
            (ACTION_CONTEXT_CONSOLIDATION, "上下文梳理", "0 4 * * *", {}),
            (ACTION_WEEKLY_REPORT, "周报", "0 18 * * 0", {}),
            (ACTION_MONTHLY_REPORT, "月报", "0 9 1 * *", {}),
            (ACTION_WEEKLY_CLEANUP, "每周整理", "0 2 * * 0", {}),
        ]
        for key, name, cron, params in defaults:
            if ScheduledJob.query.filter_by(job_key=key).first() is None:
                db.session.add(ScheduledJob(
                    job_key=key, name=name, action=key, cron=cron,
                    params=params, is_builtin=True, enabled=True,
                ))
                messages.append(f"✓ 已创建内置任务：{name}（{cron}）")
        db.session.commit()
        messages.append("初始化完成 ✔")
    return messages
