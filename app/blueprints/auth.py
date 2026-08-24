"""认证：登录 / 登出。"""
from __future__ import annotations

import threading
import time

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_limiter.util import get_remote_address
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField
from wtforms.validators import DataRequired, Length

from app.extensions import db, limiter
from app.models.user import User

bp = Blueprint("auth", __name__)

_fail_lock = threading.Lock()
_fail_log: dict[str, list[float]] = {}
_LOCKOUT_N = 8
_LOCKOUT_WINDOW = 15 * 60  # 15 分钟内失败 N 次则锁定


def _login_username_key() -> str:
    name = (request.form.get("username") or "").strip().lower()
    return f"login-user:{name or get_remote_address()}"


def _username_locked(username: str) -> bool:
    now = time.time()
    with _fail_lock:
        times = [t for t in _fail_log.get(username, []) if now - t < _LOCKOUT_WINDOW]
        _fail_log[username] = times
        return len(times) >= _LOCKOUT_N


def _record_login_fail(username: str) -> None:
    with _fail_lock:
        _fail_log.setdefault(username, []).append(time.time())


def _clear_login_fail(username: str) -> None:
    with _fail_lock:
        _fail_log.pop(username, None)


class LoginForm(FlaskForm):
    username = StringField("用户名", validators=[DataRequired(), Length(max=64)])
    password = PasswordField("密码", validators=[DataRequired(), Length(max=128)])


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
@limiter.limit("5 per minute", key_func=_login_username_key, methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    # 系统未初始化（数据库不可用/未建表/无管理员）时，提示查看安装引导
    setup_required = False
    try:
        from app.blueprints.setup import system_initialized

        setup_required = not system_initialized()
    except Exception:  # noqa: BLE001 —— 探测失败视为未初始化
        setup_required = True

    form = LoginForm()
    if form.validate_on_submit():
        uname = (form.username.data or "").strip().lower()
        if uname and _username_locked(uname):
            flash("登录失败次数过多，请 15 分钟后再试", "error")
            return render_template("auth/login.html", form=form, setup_required=setup_required)
        try:
            user = User.query.filter_by(username=form.username.data.strip()).first()
        except Exception:  # noqa: BLE001 —— 数据库不可用（尚未安装）时按登录失败处理
            user = None
        if user and user.check_password(form.password.data):
            _clear_login_fail(uname)
            login_user(user, remember=True)
            flash(f"欢迎回来，{user.username}！", "success")
            nxt = request.args.get("next") or ""
            # 仅允许站内相对路径，防开放重定向（//evil.com 与 /\evil.com 均拒绝）
            if nxt.startswith("/") and not nxt.startswith(("//", "/\\")):
                return redirect(nxt)
            return redirect(url_for("dashboard.index"))
        if uname:
            _record_login_fail(uname)
        flash("用户名或密码错误", "error")
    from_setup = request.args.get("installed") == "1"
    admin_entry = (current_app.config.get("ADMIN_ENTRY") or "").strip().strip("/")
    return render_template(
        "auth/login.html",
        form=form,
        setup_required=setup_required,
        from_setup=from_setup,
        admin_entry=admin_entry,
    )


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("已退出登录", "info")
    return redirect(url_for("auth.login"))
