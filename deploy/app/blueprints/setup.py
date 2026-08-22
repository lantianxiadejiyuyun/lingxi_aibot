"""首次安装引导页：/setup。

- 未初始化时显示可视化安装向导：①配置数据库 → ②初始化数据库 → ③创建管理员
- 数据库连接信息直接在网页上填写，保存后写入项目 .env（无需手动编辑、无需重启）
- 系统已初始化后访问自动跳转：登录用户 → 仪表盘，匿名 → 登录页
- 所有探测均容错：数据库不可用时页面仍可正常渲染引导内容
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.extensions import csrf, db

logger = logging.getLogger(__name__)

bp = Blueprint("setup", __name__)


def _db_tables_exist() -> bool:
    """数据库可连且已有数据表。"""
    try:
        from sqlalchemy import inspect

        return bool(inspect(db.engine).get_table_names())
    except Exception:  # noqa: BLE001 —— 数据库不可用视为未建表
        return False


def install_status() -> dict:
    """当前安装状态：db_ok / tables_ok / admin_ok。"""
    if not _db_tables_exist():
        return {"db_ok": False, "tables_ok": False, "admin_ok": False}
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
    return render_template("setup/index.html", status=st, dbcfg=dbcfg)


# ---------- 安装 API（未初始化期间可用） ----------

def _db_form_data() -> dict:
    data = request.get_json(silent=True) or {}
    return {
        "host": (data.get("host") or "127.0.0.1").strip(),
        "port": int(data.get("port") or 3306),
        "user": (data.get("user") or "").strip(),
        "password": data.get("password") or "",
        "db": (data.get("db") or "").strip(),
    }


@bp.route("/setup/api/db-test", methods=["POST"])
@csrf.exempt
def api_db_test():
    """测试数据库连接（不保存）。"""
    if (g := _guard_not_initialized()) is not None:
        return g
    d = _db_form_data()
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
    d = _db_form_data()
    # 密码留空且数据库当前已连通 → 保留 .env 原密码（方便只改主机/库名）
    if not d["password"] and _db_tables_exist():
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
