"""应用工厂。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from flask import Flask, redirect, render_template, request, url_for

from app.config import Config
from app.extensions import csrf, db, limiter, login_manager, migrate

# Windows 控制台默认 GBK：无法输出 ✓/emoji 等字符，会导致 flask CLI（init-db 等）崩溃。
# 统一把 stdout/stderr 切到 UTF-8（失败时静默降级，不影响无控制台环境）。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _is_flask_cli() -> bool:
    """flask CLI（init-db/migrate 等）时不启动调度器。"""
    if not sys.argv:
        return False
    argv0 = str(sys.argv[0]).lower()
    # `python -m flask ...` → ...\flask\__main__.py；`flask ...` → ...\flask.exe；`python run.py` → run.py
    return "flask" in argv0 or Path(argv0).name in ("flask", "flask.exe", "__main__.py")


def create_app(config_class=Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)

    # 反代部署（Nginx/Caddy 终结 HTTPS）时，信任 X-Forwarded-Proto 以便 Flask 生成 https 链接。
    # 仅信任 proto（x_proto=1）：不信任 X-Forwarded-Host，避免伪造 Host 绕过后台域名守卫。
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)

    # 公网部署安全告警：配置了域名但未开启 HTTPS-only cookie 时提示
    if not app.config.get("SESSION_COOKIE_SECURE"):
        if app.config.get("ADMIN_DOMAIN") or app.config.get("PAGE_DOMAIN"):
            app.logger.warning(
                "检测到已配置公网域名（ADMIN_DOMAIN/PAGE_DOMAIN），但 SESSION_COOKIE_SECURE=0："
                "公网 HTTPS 部署请设置 SESSION_COOKIE_SECURE=1，否则会话 cookie 可被中间人截获")

    logging.basicConfig(
        level=getattr(logging, app.config.get("LOG_LEVEL", "INFO")),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # ---- 扩展 ----
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    login_manager.login_view = "auth.login"
    login_manager.login_message = "请先登录"
    login_manager.login_message_category = "warning"

    from app.models.user import User

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(User, int(user_id))

    # ---- 蓝图 ----
    from app.blueprints.auth import bp as auth_bp
    from app.blueprints.dashboard import bp as dashboard_bp
    from app.blueprints.calendar import bp as calendar_bp
    from app.blueprints.tasks import bp as tasks_bp
    from app.blueprints.notes import bp as notes_bp
    from app.blueprints.notifications import bp as notifications_bp
    from app.blueprints.settings_page import bp as settings_bp
    from app.blueprints.jobs import bp as jobs_bp
    from app.blueprints.chat import bp as chat_bp
    from app.blueprints.feishu import bp as feishu_bp
    from app.blueprints.pages import bp as pages_bp, site_bp as pages_site_bp
    from app.blueprints.images import bp as images_bp, img_bp as image_files_bp
    from app.blueprints.skills import bp as skills_bp
    from app.blueprints.memory import bp as memory_bp
    from app.blueprints.setup import bp as setup_bp
    from app.blueprints.settings_api import api_bp as settings_api_bp

    for bp in (auth_bp, dashboard_bp, calendar_bp, tasks_bp, notes_bp,
               notifications_bp, settings_bp, jobs_bp, chat_bp, feishu_bp,
               pages_bp, pages_site_bp, images_bp, image_files_bp,
               skills_bp, memory_bp, setup_bp, settings_api_bp):
        app.register_blueprint(bp)

    # ---- 上下文与错误页 ----
    register_context(app)
    register_errors(app)

    # ---- CLI 命令 ----
    from app import commands

    app.cli.add_command(commands.init_db)
    app.cli.add_command(commands.create_admin)
    app.cli.add_command(commands.backup_now)
    app.cli.add_command(commands.restore_backup_cmd)
    app.cli.add_command(commands.reindex)
    app.cli.add_command(commands.list_routes)

    # ---- AI 工具加载 & 通知渠道 & 调度器 ----
    with app.app_context():
        from app.ai.registry import load_tools

        tool_modules = load_tools()
        app.logger.info("已加载 AI 工具模块: %s", tool_modules or "（无）")

        # 加载自写技能（受限沙箱，单个失败跳过不阻断启动）
        from app.services.skill_service import load_skills

        loaded_skills = load_skills()
        app.logger.info("已加载自写技能: %s", loaded_skills or "（无）")

        # 导入调度动作注册模块（briefing/backup/memory/report/maintenance 无其他导入链，必须在此显式导入）
        from app.ai import briefing  # noqa: F401
        from app.ai import report  # noqa: F401
        from app.services import backup_service  # noqa: F401
        from app.services import maintenance_service  # noqa: F401
        from app.services import memory_service  # noqa: F401

        try:
            from app.services.notify_service import load_channels

            channel_modules = load_channels()
            app.logger.info("已加载通知渠道模块: %s", channel_modules or "（无）")
        except ImportError:
            app.logger.warning("通知渠道模块尚未实现")

        app.scheduler = None
        if app.config.get("SCHEDULER_ENABLED") and not _is_flask_cli():
            try:
                from app.scheduler import SchedulerService

                app.scheduler = SchedulerService(app)
                app.scheduler.start()
            except Exception:  # noqa: BLE001 —— 数据库未初始化时给出明确提示而不是崩溃
                app.logger.exception("调度器启动失败：请先运行 flask init-db 初始化数据库")

    # ---- 静态资源缓存：第三方库/字体（含版本号、内容不可变）长期缓存，避免每次导航都重新校验导致图标闪烁 ----
    @app.after_request
    def set_static_cache_headers(response):
        if request.path.startswith("/static/vendor/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    # ---- 首次安装引导：系统未初始化（无 .env / 数据库未配置 / 未建表 / 无管理员）时，
    #      所有页面请求统一跳转到 /setup 安装向导 ----
    @app.before_request
    def _redirect_to_setup_when_uninitialized():
        if request.method != "GET" or request.path.startswith("/static/"):
            return None
        if request.endpoint == "setup.index" or (
            request.endpoint and request.endpoint.startswith("setup.")
        ):
            return None
        try:
            from app.blueprints.setup import system_initialized

            if not system_initialized():
                return redirect(url_for("setup.index"))
        except Exception:  # noqa: BLE001 —— 探测异常一律视为未初始化
            return redirect(url_for("setup.index"))
        return None

    return app


def register_context(app: Flask):
    from app.utils.timeutil import utcnow

    @app.context_processor
    def inject_globals():
        unread = 0
        try:
            from app.models.notification import Notification

            unread = Notification.query.filter_by(read=False).count()
        except Exception:  # noqa: BLE001 —— 数据库未初始化时降级
            pass
        nav = [
            ("dashboard.index", "仪表盘", "bi-speedometer2"),
            ("calendar.index", "日历", "bi-calendar3"),
            ("tasks.index", "任务", "bi-list-check"),
            ("notes.index", "笔记", "bi-journal-text"),
            ("pages.index", "网页", "bi-globe2"),
            ("images.index", "图片", "bi-image"),
            ("skills.index", "技能", "bi-puzzle"),
            ("memory.index", "记忆", "bi-lightbulb"),
            ("chat.index", "AI 助手", "bi-robot"),
            ("jobs.index", "定时任务", "bi-clock"),
            ("notifications.index", "通知", "bi-bell"),
            ("settings_page.index", "设置", "bi-gear"),
        ]
        return {"nav_items": nav, "unread_count": unread, "now_utc": utcnow()}


def register_errors(app: Flask):
    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        db.session.rollback()
        return render_template("errors/500.html"), 500
