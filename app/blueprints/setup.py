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

from flask import (
    Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for,
)
from flask_login import current_user

from app.extensions import csrf, db

logger = logging.getLogger(__name__)

bp = Blueprint("setup", __name__)


def _try_sqlalchemy_connect() -> bool:
    try:
        db.engine.connect().close()
        return True
    except Exception:  # noqa: BLE001 —— 数据库不可用视为不可连通
        return False


def _db_connectable() -> bool:
    """数据库可连通（仅探测连接，不关心是否建表）。

    保存配置后 MYSQL_* 在 .env，其它 worker / 刷新后的请求可能仍持有启动时的旧引擎。
    探测失败时从 .env 热加载一次再试，避免向导刷新回第①步。
    """
    if _try_sqlalchemy_connect():
        return True
    try:
        from app.services.install_service import reload_db_config_from_env

        if reload_db_config_from_env():
            return _try_sqlalchemy_connect()
    except Exception:  # noqa: BLE001
        logger.exception("从 .env 热加载数据库配置失败")
    return False


def _status_json(**extra):
    payload = install_status()
    payload.update(extra)
    return jsonify(payload)


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


def _delete_setup_token() -> None:
    """安装完成后删除一次性令牌文件。"""
    try:
        token_path = setup_token_path()
        if token_path.exists():
            token_path.unlink()
    except OSError:
        logger.exception("删除安装令牌失败")


def _check_token() -> bool:
    """校验请求携带的 setup_token。令牌不可持久化时返回 False（拒绝写操作）。"""
    token = _get_or_create_token()
    if not token:
        return False
    data = request.get_json(silent=True) or {}
    provided = str(data.get("setup_token") or "")
    return secrets.compare_digest(provided, token)


def _require_setup_token():
    """写操作鉴权：无法持久化令牌 → 500；令牌错误 → 403；通过 → None。"""
    if not _get_or_create_token():
        return jsonify(ok=False, error="无法持久化安装令牌（data 目录不可写），拒绝安装操作"), 500
    if not _check_token():
        return jsonify(ok=False, error="安装令牌不正确"), 403
    return None


@bp.route("/setup")
def index():
    """首次安装引导页（未初始化时显示，已初始化自动跳转）。

    向导进行中会写入 session['setup_wizard']：刚建完管理员时系统已「初始化」，
    但仍要留在本页走第④步，不能刷新后直接踢去登录。
    """
    st = install_status()
    wizard = bool(session.get("setup_wizard"))
    if st["db_ok"] and st["tables_ok"] and st["admin_ok"] and not wizard:
        if current_user.is_authenticated:
            return redirect(url_for("dashboard.index"))
        return redirect(url_for("auth.login"))
    if not (st["db_ok"] and st["tables_ok"] and st["admin_ok"]):
        session["setup_wizard"] = True
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
    if (denied := _require_setup_token()) is not None:
        return denied
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
    if (denied := _require_setup_token()) is not None:
        return denied
    d, err = _db_form_data()
    if err:
        return jsonify(ok=False, error=err), 400
    # 密码留空且数据库当前可连通（含空库）→ 保留 .env 原密码（方便只改主机/库名）
    if not d["password"] and _db_connectable():
        d["password"] = current_app.config.get("MYSQL_PASSWORD", "")

    from app.services.install_service import (
        apply_runtime_db_config, restart_scheduler, save_db_config,
    )

    try:
        save_db_config(d["host"], d["port"], d["user"], d["password"], d["db"])
        apply_runtime_db_config(d["host"], d["port"], d["user"], d["password"], d["db"])
    except PermissionError:
        return jsonify(ok=False, error="无法写入项目 .env 文件（权限不足），"
                                       "请在服务器上执行 chown/chmod 后重试")
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return jsonify(ok=False, error=f"保存失败：{e}")

    ok, err = _probe(d)
    if ok:
        restart_scheduler(current_app._get_current_object())
        session["setup_wizard"] = True
    return _status_json(ok=ok, error=err if not ok else "")


@bp.route("/setup/api/init-db", methods=["POST"])
@csrf.exempt
def api_init_db():
    """初始化数据库：建表 + 补列（管理员在第③步创建，避免刷新后被踢去登录）。"""
    if (g := _guard_not_initialized()) is not None:
        return g
    if (denied := _require_setup_token()) is not None:
        return denied
    from app.services.install_service import initialize_database, restart_scheduler

    try:
        messages = initialize_database(current_app._get_current_object(), create_admin=False)
        restart_scheduler(current_app._get_current_object())
        session["setup_wizard"] = True
        return _status_json(ok=True, messages=messages)
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return jsonify(ok=False, error=f"初始化失败：{e}")


@bp.route("/setup/api/create-admin", methods=["POST"])
@csrf.exempt
def api_create_admin():
    """创建管理员账号。"""
    if (g := _guard_not_initialized()) is not None:
        return g
    if (denied := _require_setup_token()) is not None:
        return denied
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if len(username) < 2:
        return jsonify(ok=False, error="用户名至少 2 个字符")
    if len(password) < 6:
        return jsonify(ok=False, error="密码至少 6 位")

    from app.models.user import User
    from app.services.install_service import seed_builtin_jobs_for_user, seed_first_admin_llm_from_env

    if User.query.filter_by(username=username).first():
        return jsonify(ok=False, error="用户名已存在，请换一个")
    admin = User(username=username, timezone=current_app.config["APP_TIMEZONE"], is_admin=True)
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()
    jobs = seed_builtin_jobs_for_user(admin)
    llm_notes = seed_first_admin_llm_from_env()
    session["setup_wizard"] = True
    extra = ""
    if jobs:
        extra = f"；已写入 {len(jobs)} 项内置定时任务"
    if llm_notes:
        extra += "；" + "；".join(llm_notes)
    return _status_json(ok=True, message=f"管理员 {username} 创建成功{extra}")


@bp.route("/setup/api/extra-save", methods=["POST"])
@csrf.exempt
def api_extra_save():
    """安装向导第④步：保存 AI 模型 / 域名 / 通知渠道到 .env（可跳过）。

    该步骤在管理员创建后（系统已初始化）使用，因此不走 _guard_not_initialized，
    仅靠安装令牌保护；LLM 等配置之后也可在「设置」页继续修改。
    """
    if (denied := _require_setup_token()) is not None:
        return denied
    data = request.get_json(silent=True) or {}
    from app.services.install_service import save_extra_config

    try:
        saved = save_extra_config(data)
    except PermissionError:
        return jsonify(ok=False, error="无法写入项目 .env 文件（权限不足）"), 500
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=f"保存失败：{e}"), 500
    session.pop("setup_wizard", None)
    _delete_setup_token()
    if saved:
        return jsonify(ok=True, message=f"已保存到 .env：{', '.join(saved)}（重启应用后全部生效）")
    return jsonify(ok=True, message="未填写任何配置项，已跳过")


@bp.route("/setup/api/finish", methods=["POST"])
@csrf.exempt
def api_finish():
    """结束向导（第④步跳过）：清掉 wizard 标记，之后访问 /setup 会跳登录。"""
    if system_initialized():
        session.pop("setup_wizard", None)
        _delete_setup_token()
        return jsonify(ok=True)
    if (denied := _require_setup_token()) is not None:
        return denied
    session.pop("setup_wizard", None)
    return jsonify(ok=True)
