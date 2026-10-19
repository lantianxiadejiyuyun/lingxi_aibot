"""Browser account binding for the direct QQ channel; never expose server tokens."""
from urllib.parse import urlsplit

from flask import Blueprint, current_app, flash, make_response, redirect, render_template, url_for
from flask_login import current_user, login_required

from app.extensions import db, limiter
from app.services import onebot_service, onebot_transport
from app.utils.integration_api import ApiError

bp = Blueprint("onebot", __name__, url_prefix="/settings/qq")


def _page(code=None):
    webui = current_app.config.get("ONEBOT_WEBUI_URL", "")
    try:
        parsed = urlsplit(webui)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            webui = ""
    except ValueError:
        webui = ""
    response = make_response(render_template(
        "onebot/index.html", binding=onebot_service.binding_view(current_user.id),
        connection=onebot_transport.connection_status(), binding_code=code,
        webui_url=webui if current_user.is_admin else "",
    ))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@bp.get("/")
@login_required
def index():
    return _page()


@bp.post("/code")
@login_required
@limiter.limit("20 per hour")
def create_code():
    if not onebot_transport.connection_status()["enabled"]:
        flash("请先由管理员启用 NapCat 直连服务。", "error")
        return redirect(url_for("onebot.index"))
    try:
        return _page(onebot_service.create_binding_code(current_user.id))
    except (ValueError, ApiError) as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("onebot.index"))


@bp.post("/unbind")
@login_required
def unbind():
    try:
        onebot_service.unbind(current_user.id)
        flash("已解除 QQ 绑定，并撤销尚未使用的绑定码。", "success")
    except (ValueError, ApiError) as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("onebot.index"))
