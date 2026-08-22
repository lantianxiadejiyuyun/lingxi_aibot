"""首次安装引导页：/setup。

- 未初始化时显示可视化安装向导：①配置数据库 → ②初始化数据库 → ③创建管理员
- 数据库连接信息直接在网页上填写，保存后写入项目 .env（无需手动编辑、无需重启）
- 系统已初始化后访问自动跳转：登录用户 → 仪表盘，匿名 → 登录页
- 所有探测均容错：数据库不可用时页面仍可正常渲染引导内容
- 安全：写操作需一次性安装令牌（存于服务器 data/setup_token.txt，防止首装窗口期被公网抢注）
"""
from __future__ import annotations

import logging
import secrets
from pathlib import Path

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import csrf, db

logger = logging.getLogger(__name__)

bp = Blueprint("setup", __name__)


def _db_connectable() -> bool:
    """数据库可连通（仅探测连接，不关心是否建表）。"""
    try:
        db.engine.connect().close()
        return True
    except Exception:  # noqa: BLE001 —— 数据库不可用视为不可连通
        return False


def _db_tables_exist() -> bool:
    """数据库可连且已有数据表。"""
    try:
        from sqlalchemy import inspect

        return bool(inspect(db.engine).get_table_names())
    except Exception:  # noqa: BLE001 —— 数据库不可用视为未建表
        return False


def install_status() -> dict:
    """当前安装状态：db_ok（连通）/ tables_ok（已建表）/ admin_ok。

    连通性与建表是两个独立语义：连上但为空库 → db_ok=True, tables_ok=False
    （安装向导第②步「初始化数据库」依赖该组合，不能把空库当成连不上）。
    """
    if not _db_connectable():
        return {"db_ok": False, "tables_ok": False, "admin_ok": False}
    if not _db_tables_exist():
        return {"db_ok": True, "tables_ok": False, "admin_ok": False}
    try:
        from app.models.user import User

        admin_ok = User.query.count() > 0
    except Exception:  # noqa: BLE001
        admin_ok = False
    return {"db_ok": True, "tables_ok": True, "admin_ok": admin_ok}


def system_initialized() -> bool:
    """系统是否已初始化：数据库可连 + 已有数据表 + 存在管理员账号。"""
    st = install_status()
    return st["db_ok"] and st["tables_ok"] and st["admin_ok"]


def _guard_not_initialized():
    """系统已初始化后拒绝安装 API（防止误改 .env / 重复初始化）。返回 None 表示允许。"""
    if system_initialized():
        return jsonify(ok=False, error="系统已初始化，安装接口已禁用"), 400
    return None


# ---------- 安装令牌（防首装窗口期公网抢注） ----------

def setup_token_path() -> Path:
    return Path(current_app.config.get("DATA_DIR") or Path.cwd() / "data") / "setup_token.txt"


def setup_token_hint() -> str:
    """令牌文件位置提示（页面展示用）。

    渲染安装页前先确保令牌文件已生成：否则用户打开页面时文件还不存在，
    无处读取令牌，就会卡在「先有令牌才能提交 → 提交才生成令牌」的死循环。
    """
    _get_or_create_token()
    return str(setup_token_path())


def _get_or_create_token() -> str:
    """读取/生成一次性安装令牌（只有服务器所有者可读该文件）。"""
    token_path = setup_token_path()
    try:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        if token_path.exists():
            token = token_path.read_text(encoding="utf-8").strip()
            if token:
                return token
        token = secrets.token_hex(16)
        token_path.write_text(token + "\n", encoding="utf-8")
        return token
    except OSError:
        logger.exception("无法读写安装令牌文件（data/setup_token.txt）")
        return ""


def _check_token() -> bool:
    """校验请求携带的 setup_token；令牌不可持久化时放行（仅能本机部署场景）。"""
    token = _get_or_create_token()
    if not token:
        return True  # 只读文件系统等极端场景：退化为不校验（无法被公网抢注时风险有限）
    data = request.get_json(silent=True) or {}
    provided = str(data.get("setup_token") or "")
    return secrets.compare_digest(provided, token)


@bp.route("/setup")
def index():
    """首次安装引导页（未初始化时显示，已初始化自动跳转）。"""
    if system_initialized():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard.index"))
        return redirect(url_for("auth.login"))
    st = install_status()
    # 预填当前 .env 中的数据库配置（密码不回显）
    cfg = current_app.config
    dbcfg = {
        "host": cfg.get("MYSQL_HOST", "127.0.0.1"),
        "port": cfg.get("MYSQL_PORT", 3306),
        "user": cfg.get("MYSQL_USER", ""),
        "db": cfg.get("MYSQL_DB", ""),
    }
    # 第④步预填当前 .env 配置（敏感项 API Key/密钥不回显）
    extracfg = {
        "llm_base_url": cfg.get("LLM_BASE_URL", ""),
        "llm_model": cfg.get("LLM_MODEL", ""),
        "admin_domain": cfg.get("ADMIN_DOMAIN", ""),
        "page_domain": cfg.get("PAGE_DOMAIN", ""),
        "admin_entry": cfg.get("ADMIN_ENTRY", ""),
        "default_channels": cfg.get("DEFAULT_CHANNELS", "inapp"),
    }
    return render_template("setup/index.html", status=st, dbcfg=dbcfg,
                           extracfg=extracfg, setup_token_hint=setup_token_hint())


# ---------- 安装 API（未初始化期间可用，需安装令牌） ----------

def _db_form_data() -> tuple[dict | None, str]:
    data = request.get_json(silent=True) or {}
    try:
        port = int(data.get("port") or 3306)
    except (TypeError, ValueError):
        return None, "端口必须是数字"
    return {
        "host": (data.get("host") or "127.0.0.1").strip(),
        "port": port,
        "user": (data.get("user") or "").strip(),
        "password": data.get("password") or "",
        "db": (data.get("db") or "").strip(),
    }, ""


@bp.route("/setup/api/db-test", methods=["POST"])
@csrf.exempt
def api_db_test():
    """测试数据库连接（不保存）。"""
    if (g := _guard_not_initialized()) is not None:
        return g
    if not _check_token():
        return jsonify(ok=False, error="安装令牌不正确"), 403
    d, err = _db_form_data()
    if err:
        return jsonify(ok=False, error=err), 400
    ok, err = _probe(d)
    return jsonify(ok=ok, error=err if not ok else "")


def _probe(d: dict) -> tuple[bool, str]:
    from app.services.install_service import probe_db

    return probe_db(d["host"], d["port"], d["user"], d["password"], d["db"])


@bp.route("/setup/api/db-save", methods=["POST"])
@csrf.exempt
def api_db_save():
    """保存数据库配置到 .env 并热重建引擎（无需重启进程）。"""
    if (g := _guard_not_initialized()) is not None:
        return g
    if not _check_token():
        return jsonify(ok=False, error="安装令牌不正确"), 403
    d, err = _db_form_data()
    if err:
        return jsonify(ok=False, error=err), 400
    # 密码留空且数据库当前可连通（含空库）→ 保留 .env 原密码（方便只改主机/库名）
    if not d["password"] and _db_connectable():
        d["password"] = current_app.config.get("MYSQL_PASSWORD", "")

    from app.services.install_service import rebuild_engine, restart_scheduler, save_db_config

    try:
        save_db_config(d["host"], d["port"], d["user"], d["password"], d["db"])
        rebuild_engine(d["host"], d["port"], d["user"], d["password"], d["db"])
    except PermissionError:
        return jsonify(ok=False, error="无法写入项目 .env 文件（权限不足），"
                                       "请在服务器上执行 chown/chmod 后重试")
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return jsonify(ok=False, error=f"保存失败：{e}")

    ok, err = _probe(d)
    if ok:
        restart_scheduler(current_app._get_current_object())
    return jsonify(ok=ok, error=err if not ok else "", db_ok=ok)


@bp.route("/setup/api/init-db", methods=["POST"])
@csrf.exempt
def api_init_db():
    """初始化数据库：建表 + 补列 + 管理员 + 内置任务（幂等）。"""
    if (g := _guard_not_initialized()) is not None:
        return g
    if not _check_token():
        return jsonify(ok=False, error="安装令牌不正确"), 403
    from app.services.install_service import initialize_database, restart_scheduler

    try:
        messages = initialize_database(current_app._get_current_object())
        restart_scheduler(current_app._get_current_object())
        return jsonify(ok=True, messages=messages)
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return jsonify(ok=False, error=f"初始化失败：{e}")


@bp.route("/setup/api/create-admin", methods=["POST"])
@csrf.exempt
def api_create_admin():
    """创建管理员账号。"""
    if (g := _guard_not_initialized()) is not None:
        return g
    if not _check_token():
        return jsonify(ok=False, error="安装令牌不正确"), 403
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if len(username) < 2:
        return jsonify(ok=False, error="用户名至少 2 个字符")
    if len(password) < 6:
        return jsonify(ok=False, error="密码至少 6 位")

    from app.models.user import User

    if User.query.filter_by(username=username).first():
        return jsonify(ok=False, error="用户名已存在，请换一个")
    admin = User(username=username, timezone=current_app.config["APP_TIMEZONE"])
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()
    return jsonify(ok=True, message=f"管理员 {username} 创建成功，请登录")


@bp.route("/setup/api/extra-save", methods=["POST"])
@csrf.exempt
def api_extra_save():
    """安装向导第④步：保存 AI 模型 / 域名 / 通知渠道到 .env（可跳过）。

    该步骤在管理员创建后（系统已初始化）使用，因此不走 _guard_not_initialized，
    仅靠安装令牌保护；LLM 等配置之后也可在「设置」页继续修改。
    """
    if not _check_token():
        return jsonify(ok=False, error="安装令牌不正确"), 403
    data = request.get_json(silent=True) or {}
    from app.services.install_service import save_extra_config

    try:
        saved = save_extra_config(data)
    except PermissionError:
        return jsonify(ok=False, error="无法写入项目 .env 文件（权限不足）"), 500
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=f"保存失败：{e}"), 500
    if saved:
        return jsonify(ok=True, message=f"已保存到 .env：{', '.join(saved)}（重启应用后全部生效）")
    return jsonify(ok=True, message="未填写任何配置项，已跳过")
