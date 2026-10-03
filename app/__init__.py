"""应用工厂。"""
from __future__ import annotations

import logging
import sys

from flask import Flask, redirect, render_template, request, url_for

from app.config import Config
from app.extensions import csrf, db, limiter, login_manager, migrate, recover_session

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
    argv0 = str(sys.argv[0]).lower().replace("\\", "/").rstrip("/")
    name = argv0.rsplit("/", 1)[-1]
    # `python -m flask ...` → .../flask/__main__.py；`flask ...` → .../flask.exe；
    # 按文件名精确匹配，避免工作目录路径含 "flask" 子串误判
    return name in ("flask", "flask.exe") or argv0.endswith("flask/__main__.py")


def apply_admin_entry_config(app: Flask, entry: str | None = None) -> str:
    """同步后台短入口，并把会话 Cookie 限定在入口路径下。

    公开网页走 /webs/html/<slug>，不带此后缀，因此不会拿到后台 Cookie。
    """
    if entry is None:
        entry = str(app.config.get("ADMIN_ENTRY") or "")
    entry = entry.strip().strip("/")
    app.config["ADMIN_ENTRY"] = entry
    app.config["SESSION_COOKIE_PATH"] = f"/{entry}" if entry else "/"
    return entry


def create_app(config_class=Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)
    apply_admin_entry_config(app)

    # 反代部署（Nginx/Caddy 终结 HTTPS）时，信任 X-Forwarded-Proto 以便 Flask 生成 https 链接。
    # 仅信任 proto（x_proto=1）：不信任 X-Forwarded-Host（访问不再按域名分流）。
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)

    # ---- 后台短入口：配置 ADMIN_ENTRY 后，后台藏于 /<入口> 前缀下 ----
    _entry_inner = app.wsgi_app

    def _admin_entry_middleware(environ, start_response):
        entry = (app.config.get("ADMIN_ENTRY") or "").strip().strip("/")
        if not entry:
            return _entry_inner(environ, start_response)

        path = environ.get("PATH_INFO") or "/"

        def _pass():
            return _entry_inner(environ, start_response)

        def _is(prefix: str) -> bool:
            return path == prefix or path.startswith(prefix + "/")

        # 飞书 / 探活 / 公开网页 / 静态资源：免短入口（局域网 IP 可直达）
        if _is("/feishu") or path == "/healthz" or _is("/webs/html") or _is("/p") \
                or _is("/static") or _is("/img") or _is("/setup"):
            return _pass()

        prefix = "/" + entry
        if path == prefix:
            environ["SCRIPT_NAME"] = (environ.get("SCRIPT_NAME") or "") + prefix
            environ["PATH_INFO"] = "/"
            environ["aibot.admin_entry_passed"] = "1"
            return _pass()
        if path.startswith(prefix + "/"):
            environ["SCRIPT_NAME"] = (environ.get("SCRIPT_NAME") or "") + prefix
            environ["PATH_INFO"] = path[len(prefix):]
            environ["aibot.admin_entry_passed"] = "1"
            return _pass()

        start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"Not Found"]

    app.wsgi_app = _admin_entry_middleware

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

    @app.teardown_appcontext
    def _rollback_db_session(exc):
        if exc is not None:
            recover_session()

    login_manager.login_view = "auth.login"
    login_manager.login_message = "请先登录"
    login_manager.login_message_category = "warning"

    from app.models.user import User

    @login_manager.user_loader
    def load_user(user_id: str):
        try:
            return db.session.get(User, int(user_id))
        except (TypeError, ValueError):
            return None

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
    from app.blueprints.fitness import bp as fitness_bp
    from app.blueprints.travel import bp as travel_bp
    from app.blueprints.expenses import bp as expenses_bp
    from app.blueprints.expenses_api import api_bp as expenses_api_bp
    from app.blueprints.setup import bp as setup_bp
    from app.blueprints.settings_api import api_bp as settings_api_bp
    from app.blueprints.integration_account import bp as integration_account_bp
    from app.blueprints.integration_data import bp as integration_data_bp
    from app.blueprints.integration_chat import bp as integration_chat_bp
    from app.blueprints.integration_vault import bp as integration_vault_bp
    from app.blueprints.navigation_vault import bp as navigation_vault_bp

    for bp in (auth_bp, dashboard_bp, calendar_bp, tasks_bp, notes_bp,
               notifications_bp, settings_bp, jobs_bp, chat_bp, feishu_bp,
               pages_bp, pages_site_bp, images_bp, image_files_bp,
               skills_bp, memory_bp, fitness_bp, travel_bp, expenses_bp,
               expenses_api_bp, setup_bp, settings_api_bp, navigation_vault_bp):
        app.register_blueprint(bp)

    # Dedicated integration endpoints accept Tokens, never browser-session auth.
    for bp in (integration_account_bp, integration_data_bp, integration_chat_bp, integration_vault_bp):
        csrf.exempt(bp)
        app.register_blueprint(bp)

    from app.services.integration_ws import init_integration_ws
    init_integration_ws(app)

    # ---- 上下文与错误页 ----
    register_context(app)
    register_errors(app)

    # ---- CLI 命令 ----
    from app import commands

    app.cli.add_command(commands.init_db)
    app.cli.add_command(commands.create_admin)
    app.cli.add_command(commands.reset_db)
    app.cli.add_command(commands.reset_admin_password)
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

        # 飞书官方 SDK 长连接（配置为 sdk 且非 CLI 时启动）
        if not _is_flask_cli():
            try:
                from app.services.feishu_ws import start_if_needed

                start_if_needed(app)
            except Exception:  # noqa: BLE001
                app.logger.exception("飞书 SDK 长连接启动失败")
            try:
                from app.services.page_site_server import start_if_needed as start_page_site

                start_page_site(app)
            except Exception:  # noqa: BLE001
                app.logger.exception("网页站点端口启动失败")

    @app.route("/healthz")
    def healthz():
        """探活：未登录、无数据库依赖，始终 200。供 Docker healthcheck / 负载均衡使用。"""
        return ("ok", 200, {"Content-Type": "text/plain; charset=utf-8"})

    # ---- 静态资源缓存：第三方库/字体（含版本号、内容不可变）长期缓存，避免每次导航都重新校验导致图标闪烁 ----
    @app.after_request
    def set_static_cache_headers(response):
        if request.path == "/api/v1" or request.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "no-store"
        if request.path.startswith("/static/vendor/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response

    # ---- 首次安装引导：系统未初始化（无 .env / 数据库未配置 / 未建表 / 无管理员）时，
    #      所有页面请求统一跳转到 /setup 安装向导 ----
    @app.before_request
    def _redirect_to_setup_when_uninitialized():
        # Integrations must receive JSON status codes, never installation HTML.
        if request.path == "/api/v1" or request.path.startswith("/api/v1/"):
            return None
        if request.method != "GET" or request.path.startswith("/static/"):
            return None
        if request.path.rstrip("/") == "/healthz":
            return None
        if request.endpoint == "setup.index" or (
            request.endpoint and request.endpoint.startswith("setup.")
        ):
            return None
        # 已登录用户：数据库短暂抖动时不应被踢到安装页（仅未初始化场景才引导）
        from flask_login import current_user

        if current_user.is_authenticated:
            return None
        try:
            from app.blueprints.setup import system_initialized

            if not system_initialized():
                return redirect(url_for("setup.index"))
        except Exception:  # noqa: BLE001 —— 探测异常一律视为未初始化
            recover_session()
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
            recover_session()
        nav_groups = [
            ("", [
                ("dashboard.index", "仪表盘", "bi-speedometer2"),
                ("chat.index", "AI 助手", "bi-robot"),
            ]),
            ("工作", [
                ("calendar.index", "日历", "bi-calendar3"),
                ("tasks.index", "任务", "bi-list-check"),
                ("notes.index", "笔记", "bi-journal-text"),
                ("pages.index", "网页", "bi-globe2"),
                ("images.index", "图片", "bi-image"),
            ]),
            ("生活", [
                ("fitness.index", "健身", "bi-heart-pulse"),
                ("travel.index", "出行", "bi-airplane"),
                ("expenses.index", "消费", "bi-wallet2"),
            ]),
            ("智能", [
                ("skills.index", "技能", "bi-puzzle"),
                ("memory.index", "记忆", "bi-lightbulb"),
            ]),
            ("系统", [
                ("jobs.index", "定时任务", "bi-clock"),
                ("notifications.index", "通知", "bi-bell"),
                ("settings_page.index", "设置", "bi-gear"),
            ]),
        ]
        nav_items = [item for _, items in nav_groups for item in items]
        ui_theme = ""
        try:
            from flask_login import current_user as _cu

            from app.models.user import DEFAULT_THEME, THEMES

            if getattr(_cu, "is_authenticated", False):
                theme = _cu.get_theme()
                ui_theme = theme if theme in THEMES else DEFAULT_THEME
        except Exception:  # noqa: BLE001 —— 未登录或库未初始化
            recover_session()
        return {
            "nav_groups": nav_groups,
            "nav_items": nav_items,
            "unread_count": unread,
            "now_utc": utcnow(),
            "ui_theme": ui_theme,
        }


def register_errors(app: Flask):
    from werkzeug.exceptions import HTTPException

    @app.errorhandler(HTTPException)
    def http_error(e):
        if request.path == "/api/v1" or request.path.startswith("/api/v1/"):
            from app.utils.integration_api import error_response
            return error_response(e.name, e.code or 500, "http_error")
        return e

    @app.errorhandler(404)
    def not_found(e):
        if request.path == "/api/v1" or request.path.startswith("/api/v1/"):
            from app.utils.integration_api import error_response
            return error_response("接口或资源不存在", 404, "not_found")
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        recover_session()
        if request.path == "/api/v1" or request.path.startswith("/api/v1/"):
            from app.utils.integration_api import error_response
            return error_response("服务暂时不可用，请稍后重试", 500, "internal_error")
        return render_template("errors/500.html"), 500
