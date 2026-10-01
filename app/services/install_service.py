"""安装服务：网页安装向导的核心逻辑。

- probe_db：直接探测 MySQL 连通性（区分"没启动/地址错""账号密码错""库不存在"等）
- save_db_config：把数据库配置写入运行 .env（供网页向导保存）
- rebuild_engine：热重建 SQLAlchemy 引擎（改配置后无需重启进程）
- initialize_database：建表 + 补列 + 管理员 + 内置任务（幂等，CLI 与网页共用）
- drop_all_tables：DROP 当前库全部数据表（安装向导重置用，不删库、不改 .env）
- restart_scheduler：数据库就绪后补启动调度器（首次启动连不上库时调度器会启动失败）
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import quote

import pymysql
from dotenv import dotenv_values, set_key

from app.config import ENV_PATH
from app.extensions import db

logger = logging.getLogger(__name__)

# 项目根目录（app/services/install_service.py → 上三级）
BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _ensure_env_file() -> None:
    """.env 不存在时创建空文件（set_key 需要目标文件存在）。"""
    if not ENV_PATH.exists():
        ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
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
    """把数据库配置写入 ENV_PATH 指定的运行配置。返回提示消息。"""
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
    return f"已保存到运行配置 {ENV_PATH.name}"


def apply_runtime_db_config(host: str, port, user: str, password: str, db_name: str) -> None:
    """把 MySQL 配置写入当前进程（app.config + os.environ）并热重建引擎。

    只写 .env 不够：Flask 启动时已经把 MYSQL_* 读进内存，保存后若不更新，
    下一请求仍按旧连接串探测，安装向导会刷新回第①步。
    """
    from flask import current_app

    host = (host or "127.0.0.1").strip()
    port = int(port or 3306)
    user = (user or "").strip()
    password = password or ""
    db_name = (db_name or "").strip()
    app = current_app._get_current_object()
    app.config["MYSQL_HOST"] = host
    app.config["MYSQL_PORT"] = port
    app.config["MYSQL_USER"] = user
    app.config["MYSQL_PASSWORD"] = password
    app.config["MYSQL_DB"] = db_name
    os.environ["MYSQL_HOST"] = host
    os.environ["MYSQL_PORT"] = str(port)
    os.environ["MYSQL_USER"] = user
    os.environ["MYSQL_PASSWORD"] = password
    os.environ["MYSQL_DB"] = db_name
    rebuild_engine(host, port, user, password, db_name)


def reload_db_config_from_env() -> bool:
    """若 .env 里的 MYSQL_* 与内存不一致，热加载并重建引擎。有变更返回 True。"""
    from flask import current_app

    if not ENV_PATH.exists():
        return False
    vals = dotenv_values(ENV_PATH) or {}
    host = (vals.get("MYSQL_HOST") or "127.0.0.1").strip()
    user = (vals.get("MYSQL_USER") or "").strip()
    password = vals.get("MYSQL_PASSWORD") or ""
    db_name = (vals.get("MYSQL_DB") or "").strip()
    try:
        port = int(vals.get("MYSQL_PORT") or 3306)
    except (TypeError, ValueError):
        port = 3306
    if not user and not db_name:
        return False
    app = current_app._get_current_object()
    current = (
        str(app.config.get("MYSQL_HOST") or ""),
        int(app.config.get("MYSQL_PORT") or 0),
        str(app.config.get("MYSQL_USER") or ""),
        str(app.config.get("MYSQL_PASSWORD") or ""),
        str(app.config.get("MYSQL_DB") or ""),
    )
    if current == (host, port, user, password, db_name):
        return False
    apply_runtime_db_config(host, port, user, password, db_name)
    return True


# 安装向导第④步可写入的配置项（键名 → .env 变量）；只收字符串，空值跳过不覆盖
EXTRA_ENV_FIELDS: dict[str, str] = {
    "llm_base_url": "LLM_BASE_URL",
    "llm_api_key": "LLM_API_KEY",
    "llm_model": "LLM_MODEL",
    "admin_entry": "ADMIN_ENTRY",
    "sc_key": "SC_KEY",
    "feishu_webhook_url": "FEISHU_WEBHOOK_URL",
    "feishu_secret": "FEISHU_SECRET",
    "default_channels": "DEFAULT_CHANNELS",
}


def save_extra_config(data: dict) -> list[str]:
    """安装向导「基础配置」步骤：把 AI/短入口/通知渠道写入 .env。

    只写入用户实际填写（非空）的项；返回已保存的变量名列表（用于页面提示）。
    短入口同时刷新到 current_app.config，立即生效（无需重启）。
    """
    from flask import current_app

    _ensure_env_file()
    saved: list[str] = []
    for field, env_key in EXTRA_ENV_FIELDS.items():
        value = str(data.get(field) or "").strip()
        if value:
            set_key(ENV_PATH, env_key, value, quote_mode="always")
            saved.append(env_key)
            # 中间件 / 路由守卫运行时读 current_app.config，这里同步刷新，立即生效
            if env_key == "ADMIN_ENTRY":
                try:
                    entry = value.strip().strip("/")
                    current_app.config["ADMIN_ENTRY"] = entry
                    current_app.config["SESSION_COOKIE_PATH"] = f"/{entry}" if entry else "/"
                except RuntimeError:
                    pass  # 无应用上下文时忽略
    return saved


def rebuild_engine(host: str, port, user: str, password: str, db_name: str) -> None:
    """热重建 SQLAlchemy 引擎（无需重启进程，下次 DB 访问用新配置）。

    实现：对齐 Flask-SQLAlchemy init_app 的默认引擎构建逻辑，
    替换 _app_engines 缓存中的默认引擎并 dispose 旧引擎。
    """
    from flask import current_app

    app = current_app._get_current_object()
    # quote（空格→%20）而非 quote_plus：SQLAlchemy unquote 不解码 +
    uri = (f"mysql+pymysql://{quote(user, safe='')}:{quote(password, safe='')}"
           f"@{quote(str(host or '127.0.0.1').strip(), safe=':.')}:{int(port or 3306)}"
           f"/{quote(str(db_name or '').strip(), safe='')}?charset=utf8mb4")
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
    """已有库补齐新增列（db.create_all 不会给已存在的表加列）。含多用户 user_id 迁移。"""
    from sqlalchemy import inspect, text

    insp = inspect(db.engine)
    if "events" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("events")}
        if "last_reminded_occurrence_utc" not in cols:
            db.session.execute(text(
                "ALTER TABLE events ADD COLUMN last_reminded_occurrence_utc DATETIME NULL"))
            if messages is not None:
                messages.append("✓ 已为 events 表补充日程提醒去重字段")
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
    # 出行计划：早期版本是单一 transport 列，后改为 transports/segments/alternatives 多段交通
    if "trip_plans" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("trip_plans")}
        for col, ddl in (
            ("transports", "ALTER TABLE trip_plans ADD COLUMN transports JSON NULL"),
            ("segments", "ALTER TABLE trip_plans ADD COLUMN segments JSON NULL"),
            ("alternatives", "ALTER TABLE trip_plans ADD COLUMN alternatives JSON NULL"),
        ):
            if col not in cols:
                db.session.execute(text(ddl))
                if messages is not None:
                    messages.append(f"✓ 已为 trip_plans 表补充 {col} 列")

    # ---- 多用户：给个人数据表补 user_id 列并回填到第一个用户 ----
    _migrate_multi_user(messages)
    db.session.commit()


def _migrate_multi_user(messages: list[str] | None = None) -> None:
    """多用户迁移：加 user_id 列 + is_admin 列 + 回填 + 去掉 scheduled_jobs.job_key 唯一约束。"""
    from sqlalchemy import inspect, text

    insp = inspect(db.engine)
    tables = insp.get_table_names()

    # users 加 is_admin / feishu_open_id（飞书发送者绑定）
    if "users" in tables:
        cols = {c["name"] for c in insp.get_columns("users")}
        if "is_admin" not in cols:
            db.session.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"))
            if messages is not None:
                messages.append("✓ 已为 users 表补充 is_admin 列")
        if "feishu_open_id" not in cols:
            db.session.execute(text("ALTER TABLE users ADD COLUMN feishu_open_id VARCHAR(64) NULL"))
            if messages is not None:
                messages.append("✓ 已为 users 表补充 feishu_open_id 列")
        if "api_token" not in cols:
            db.session.execute(text("ALTER TABLE users ADD COLUMN api_token VARCHAR(128) NULL"))
            if messages is not None:
                messages.append("✓ 已为 users 表补充 api_token 列")
        # feishu_open_id / api_token 唯一约束（NULL 可多个）
        try:
            uniqs = insp.get_unique_constraints("users")
            uniq_cols = {tuple(u.get("column_names") or []) for u in uniqs}
            if ("feishu_open_id",) not in uniq_cols and not any(
                    "feishu_open_id" in (u.get("column_names") or []) for u in uniqs):
                db.session.execute(text(
                    "ALTER TABLE users ADD UNIQUE KEY uq_users_feishu_open_id (feishu_open_id)"))
            if not any("api_token" in (u.get("column_names") or []) for u in uniqs):
                db.session.execute(text(
                    "ALTER TABLE users ADD UNIQUE KEY uq_users_api_token (api_token)"))
        except Exception:  # noqa: BLE001
            pass

    # scheduled_jobs 去掉 job_key 唯一约束（多用户下每个用户同名任务）
    if "scheduled_jobs" in tables:
        try:
            uniqs = insp.get_unique_constraints("scheduled_jobs")
            for u in uniqs:
                if "job_key" in (u.get("column_names") or []):
                    name = u.get("name") or "job_key"
                    db.session.execute(text(f"ALTER TABLE scheduled_jobs DROP INDEX {name}"))
                    if messages is not None:
                        messages.append("✓ 已移除 scheduled_jobs.job_key 唯一约束")
        except Exception:  # noqa: BLE001
            pass

    # settings 表：旧结构是 key 主键，改为 id 主键 + user_id（0=全局）
    if "settings" in tables:
        cols = {c["name"] for c in insp.get_columns("settings")}
        if "id" not in cols:
            try:
                db.session.execute(text("ALTER TABLE settings DROP PRIMARY KEY"))
            except Exception:  # noqa: BLE001
                pass
            db.session.execute(text(
                "ALTER TABLE settings ADD COLUMN id INT AUTO_INCREMENT PRIMARY KEY"))
            if messages is not None:
                messages.append("✓ 已为 settings 表补充 id 主键")
        if "user_id" not in cols:
            db.session.execute(text(
                "ALTER TABLE settings ADD COLUMN user_id INT NOT NULL DEFAULT 0"))
            if messages is not None:
                messages.append("✓ 已为 settings 表补充 user_id 列")
        try:
            insp2 = inspect(db.engine)
            uniqs = insp2.get_unique_constraints("settings")
            has = any("user_id" in (u.get("column_names") or []) and "key" in (u.get("column_names") or [])
                      for u in uniqs)
            if not has:
                db.session.execute(text(
                    "ALTER TABLE settings ADD UNIQUE KEY uq_setting_key_user (key, user_id)"))
        except Exception:  # noqa: BLE001
            pass

    # 个人数据表加 user_id
    user_tables = [
        "events", "tasks", "notes", "conversations", "notifications", "webpages",
        "image_assets", "memories", "embeddings", "fitness_records", "trip_plans",
        "expense_records", "scheduled_jobs",
    ]
    for t in user_tables:
        if t not in tables:
            continue
        cols = {c["name"] for c in insp.get_columns(t)}
        if "user_id" not in cols:
            db.session.execute(text(f"ALTER TABLE {t} ADD COLUMN user_id INTEGER NULL"))
            if messages is not None:
                messages.append(f"✓ 已为 {t} 表补充 user_id 列")

    # 回填：把无主数据挂到第一个用户，并设其为管理员
    from app.models.user import User

    first = User.query.order_by(User.id.asc()).first()
    if first is None:
        return
    if "users" in tables:
        db.session.execute(text("UPDATE users SET is_admin = 1 WHERE is_admin = 0 AND id = :uid"),
                           {"uid": first.id})
    for t in user_tables:
        if t in tables:
            db.session.execute(text(f"UPDATE {t} SET user_id = :uid WHERE user_id IS NULL"),
                               {"uid": first.id})


def _builtin_jobs() -> list[tuple]:
    """内置任务默认定义（job_key, 名称, cron, params）。"""
    from app.models.scheduled_job import (
        ACTION_CONTEXT_CONSOLIDATION, ACTION_DATA_BACKUP, ACTION_EVENT_REMINDER,
        ACTION_EVENING_REVIEW, ACTION_FITNESS_REMINDER, ACTION_FITNESS_WEEKLY_ANALYSIS,
        ACTION_MONTHLY_REPORT, ACTION_MORNING_BRIEFING, ACTION_NOON_BRIEFING,
        ACTION_TASK_DUE, ACTION_TRIP_REMINDER, ACTION_WEEKLY_CLEANUP, ACTION_WEEKLY_REPORT,
    )
    return [
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
        (ACTION_FITNESS_REMINDER, "健身提醒", "0 20 * * *", {}),
        (ACTION_FITNESS_WEEKLY_ANALYSIS, "健身周分析", "0 19 * * 0", {}),
        (ACTION_TRIP_REMINDER, "出行提醒", "0 9 * * *", {}),
    ]


def seed_builtin_jobs_for_user(user) -> list[str]:
    """为指定用户补齐内置定时任务（幂等），返回新建任务名列表。"""
    from app.models.scheduled_job import ScheduledJob

    created: list[str] = []
    for key, name, cron, params in _builtin_jobs():
        if ScheduledJob.query.filter_by(job_key=key, user_id=user.id).first() is None:
            db.session.add(ScheduledJob(
                job_key=key, name=name, action=key, cron=cron,
                params=params, is_builtin=True, enabled=True, user_id=user.id,
            ))
            created.append(name)
    db.session.commit()
    return created


def drop_all_tables() -> list[str]:
    """DROP 当前连接库中的全部数据表。不删库、不改 .env、不碰磁盘文件。

    返回已删除的表名；空库返回空列表。MySQL 下先关外键检查再一条 DROP，
    以便循环外键也能删干净。DDL 基本无法事务回滚：失败时可能已删掉一部分。
    """
    from sqlalchemy import inspect, text

    insp = inspect(db.engine)
    tables = list(insp.get_table_names())
    if not tables:
        return []
    preparer = db.engine.dialect.identifier_preparer
    quoted = ", ".join(preparer.quote(name) for name in tables)
    is_mysql = db.engine.dialect.name == "mysql"
    # DDL 走引擎连接，避免和 session 事务搅在一起（MySQL DROP 会隐式提交）
    try:
        with db.engine.connect() as conn:
            if is_mysql:
                conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            conn.execute(text(f"DROP TABLE IF EXISTS {quoted}"))
            if is_mysql:
                conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
            conn.commit()
    except Exception:
        if is_mysql:
            try:
                with db.engine.connect() as conn:
                    conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
                    conn.commit()
            except Exception:  # noqa: BLE001
                pass
        raise
    finally:
        try:
            insp.clear_cache()
        except Exception:  # noqa: BLE001
            pass
        db.session.remove()
    logger.info("已删除数据表 %d 张：%s", len(tables), ", ".join(tables))
    return tables


def initialize_database(app, *, create_admin: bool = True) -> list[str]:
    """建表 + 补列 + 初始化管理员/内置定时任务（幂等）。返回过程消息列表。

    create_admin=False：网页向导第②步只用建表，管理员留给第③步，避免刷新后
    被当成「已初始化」直接踢去登录页。
    """
    messages: list[str] = []
    with app.app_context():
        db.create_all()
        messages.append("✓ 数据表已创建")
        ensure_schema(messages)

        from app.models.user import User

        cfg = app.config
        if User.query.count() == 0:
            if create_admin:
                import secrets

                admin = User(username=cfg["ADMIN_USERNAME"], timezone=cfg["APP_TIMEZONE"],
                             is_admin=True)
                password = str(cfg.get("ADMIN_PASSWORD") or "").strip()
                if password:
                    admin.set_password(password)
                    messages.append(f"✓ 已创建管理员：{cfg['ADMIN_USERNAME']}"
                                    "（请登录后立即修改密码）")
                else:
                    # 未显式配置密码：生成随机密码并打印一次，避免弱默认口令被利用
                    password = secrets.token_urlsafe(12)
                    admin.set_password(password)
                    messages.append(f"✓ 已创建管理员：{cfg['ADMIN_USERNAME']}"
                                    f"（随机初始密码：{password}，请立即登录修改）")
                db.session.add(admin)
                db.session.commit()
            else:
                messages.append("✓ 数据表已就绪，请继续创建管理员")
        else:
            # 兼容旧数据：首个用户设为管理员
            first = User.query.order_by(User.id.asc()).first()
            if first is not None and not first.is_admin:
                first.is_admin = True
                db.session.commit()
            messages.append("✓ 管理员已存在，跳过")

        # 每个用户一套内置任务（job_key 相同，按 user_id 区分）
        for user in User.query.order_by(User.id.asc()).all():
            created = seed_builtin_jobs_for_user(user)
            for name in created:
                messages.append(f"✓ 已为用户 {user.username} 创建内置任务：{name}")

        if User.query.count() > 0:
            migrated = seed_first_admin_llm_from_env()
            if migrated:
                messages.extend(migrated)
        messages.append("初始化完成 ✔")
    return messages


def seed_first_admin_llm_from_env() -> list[str]:
    """把 .env / 全局 settings 里的 LLM Key 只拷给「还没有自己 Key」的第一个管理员。

    对话模型 Key 按用户隔离，其它账号不会继承。已有用户级 Key 的不覆盖。
    """
    from flask import current_app

    from app.models.setting import Setting
    from app.models.user import User
    from app.services.settings_service import get_own_setting, set_setting

    admin = User.query.filter_by(is_admin=True).order_by(User.id.asc()).first()
    if admin is None:
        admin = User.query.order_by(User.id.asc()).first()
    if admin is None:
        return []

    notes: list[str] = []
    own_key = str(get_own_setting("llm_api_key", "", user_id=admin.id) or "").strip()
    if not own_key:
        env_key = str(current_app.config.get("LLM_API_KEY") or "").strip()
        global_row = Setting.query.filter_by(key="llm_api_key", user_id=0).first()
        global_key = str((global_row.value if global_row is not None else "") or "").strip()
        source = env_key or global_key
        if source:
            set_setting("llm_api_key", source, user_id=admin.id)
            notes.append(f"✓ 已将环境/全局 LLM Key 写入管理员 {admin.username} 的个人设置（其他用户需自行配置）")

    # 协议 / 地址 / 模型：管理员未填时用 .env 占位，仍不共享 Key
    for db_key, env_key in (
        ("llm_protocol", "LLM_PROTOCOL"),
        ("llm_base_url", "LLM_BASE_URL"),
        ("llm_model", "LLM_MODEL"),
    ):
        if str(get_own_setting(db_key, "", user_id=admin.id) or "").strip():
            continue
        env_val = str(current_app.config.get(env_key) or "").strip()
        if env_val:
            set_setting(db_key, env_val, user_id=admin.id)

    # 清掉全局 Key，避免旧代码 get_setting 回退时串号
    leaked = Setting.query.filter_by(key="llm_api_key", user_id=0).all()
    for row in leaked:
        if row.value:
            row.value = ""
            notes.append("✓ 已清除全局 llm_api_key（改由各用户自己的设置保存）")
    db.session.commit()
    return notes
