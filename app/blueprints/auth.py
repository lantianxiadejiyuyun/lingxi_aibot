"""认证：登录 / 登出。"""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField
from wtforms.validators import DataRequired, Length

from app.extensions import db, limiter
from app.models.user import User

bp = Blueprint("auth", __name__)


class LoginForm(FlaskForm):
    username = StringField("用户名", validators=[DataRequired(), Length(max=64)])
    password = PasswordField("密码", validators=[DataRequired(), Length(max=128)])


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
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
        try:
            user = User.query.filter_by(username=form.username.data.strip()).first()
        except Exception:  # noqa: BLE001 —— 数据库不可用（尚未安装）时按登录失败处理
            user = None
        if user and user.check_password(form.password.data):
            login_user(user, remember=True)
            flash(f"欢迎回来，{user.username}！", "success")
            nxt = request.args.get("next") or ""
            # 仅允许站内相对路径，防开放重定向（//evil.com 与 /\evil.com 均拒绝）
            if nxt.startswith("/") and not nxt.startswith(("//", "/\\")):
                return redirect(nxt)
            return redirect(url_for("dashboard.index"))
        flash("用户名或密码错误", "error")
    return render_template("auth/login.html", form=form, setup_required=setup_required)


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("已退出登录", "info")
    return redirect(url_for("auth.login"))
