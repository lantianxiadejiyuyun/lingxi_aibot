"""网页生成器：管理（列表/在线编辑/复制/删除/显示开关）+ 公开访问。

访问（不再按域名分流，局域网 IP / 任意 Host 均可）：
- 后台：/<ADMIN_ENTRY>/…（短入口，未配置入口时走根路径）
- 公开网页：/webs/html/<slug>
- 私有网页：登录后同一路径；配置了短入口时为 /<入口>/webs/html/<slug>
- 旧路径 /p/<slug> 301 到 /webs/html/<slug>
"""
from __future__ import annotations

from flask import (
    Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template,
    request, url_for,
)
from flask_login import current_user, login_required

from app.services import page_service
from app.utils.timeutil import fmt_dt, user_tz

bp = Blueprint("pages", __name__, url_prefix="/pages")
site_bp = Blueprint("pages_site", __name__)


def _render_page(page) -> Response:
    """把页面 content 作为 HTML 渲染输出。"""
    resp = Response(page.content or "", mimetype="text/html; charset=utf-8")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _admin_entry() -> str:
    """配置的后台短入口（未配置返回空串）。"""
    return (current_app.config.get("ADMIN_ENTRY") or "").strip().strip("/")


@site_bp.before_app_request
def _short_entry_routing():
    """配置短入口后：未带入口的请求只放行公开网页 / 静态资源 / 飞书 / 探活。"""
    if not _admin_entry():
        return None
    if request.environ.get("aibot.admin_entry_passed") == "1":
        return None
    path = request.path.strip("/")
    if path == "healthz" or path.startswith(("static/", "img/", "feishu/", "webs/html/", "p/")):
        return None
    if path in ("setup",) or path.startswith("setup/"):
        return None
    abort(404)


@site_bp.route("/webs/html/<slug>")
def public_view(slug):
    """公开页任何人可访问；私有页需登录（属主或管理员）。"""
    page = page_service.visible_by_slug(slug)
    if page is None:
        abort(404)
    if page.is_public:
        return _render_page(page)
    if not current_user.is_authenticated:
        abort(404)
    if page.user_id != current_user.id and not getattr(current_user, "is_admin", False):
        abort(404)
    return _render_page(page)


@site_bp.route("/p/<slug>")
def legacy_p_view(slug):
    """兼容旧地址 /p/<slug> → /webs/html/<slug>。"""
    page = page_service.visible_by_slug(slug)
    target = page_service.page_url_path(slug)
    if page is not None and not page.is_public:
        entry = _admin_entry()
        if entry:
            target = f"/{entry}{target}"
    return redirect(target, code=301)


# ---------- 管理页 ----------

def _page_view(page) -> dict:
    """页面 → 模板用 dict（含访问地址）。"""
    tz = user_tz(current_user)
    return {
        "id": page.id,
        "title": page.title,
        "slug": page.slug,
        "description": page.description,
        "content": page.content,
        "is_public": page.is_public,
        "enabled": page.enabled,
        "updated_at": fmt_dt(page.updated_at, tz),
        "url": page_service.page_public_url(page),
    }


@bp.route("/")
@login_required
def index():
    """网页列表页。"""
    pages = page_service.list_pages(current_user.id)
    items = [_page_view(p) for p in pages]
    return render_template(
        "pages/index.html",
        pages=items,
        page_base_url=page_service.page_site_base_url(),
    )


@bp.route("/edit", defaults={"page_id": None}, methods=["GET"])
@bp.route("/edit/<int:page_id>", methods=["GET"])
@login_required
def edit(page_id):
    """在线编辑器：page_id 为空时新建（默认模板），否则编辑现有页面。"""
    if page_id is None:
        page = {
            "id": None, "title": "", "slug": "", "description": "",
            "content": page_service.default_template("新页面"),
            "is_public": True, "enabled": True,
        }
    else:
        row = page_service.get_page(page_id, current_user.id)
        if row is None:
            flash("网页不存在或已删除", "error")
            return redirect(url_for("pages.index"))
        page = _page_view(row)
    return render_template("pages/edit.html", page=page)


@bp.route("/api/save", methods=["POST"])
@login_required
def api_save():
    """新建或更新页面（JSON）：page_id 为空新建，否则更新。"""
    data = request.get_json(silent=True) or {}
    page_id = data.get("page_id")
    try:
        if page_id in (None, ""):
            page = page_service.create_page(
                current_user.id,
                title=data.get("title") or "",
                content=data.get("content") or "",
                slug=(data.get("slug") or "").strip() or None,
                description=data.get("description") or "",
                is_public=bool(data.get("is_public", True)),
                enabled=bool(data.get("enabled", True)),
            )
        else:
            page = page_service.get_page(page_id, current_user.id)
            if page is None:
                return jsonify({"ok": False, "error": "网页不存在或已删除"}), 400
            page = page_service.update_page(
                page,
                title=data.get("title"),
                content=data.get("content"),
                slug=(data.get("slug") or "").strip() or None,
                description=data.get("description"),
                is_public=data.get("is_public"),
                enabled=data.get("enabled"),
            )
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "data": {"id": page.id, "slug": page.slug,
                                         "url": page_service.page_public_url(page)}})


@bp.route("/api/toggle", methods=["POST"])
@login_required
def api_toggle():
    """显示/隐藏开关：enabled=false 时前台 404（后台仍可编辑）。"""
    data = request.get_json(silent=True) or {}
    page = page_service.get_page(data.get("page_id"), current_user.id)
    if page is None:
        return jsonify({"ok": False, "error": "网页不存在或已删除"}), 400
    page_service.update_page(page, enabled=bool(data.get("enabled", True)))
    return jsonify({"ok": True, "data": {"id": page.id, "enabled": page.enabled}})


@bp.route("/api/duplicate", methods=["POST"])
@login_required
def api_duplicate():
    """复制页面为新页面。"""
    data = request.get_json(silent=True) or {}
    page = page_service.get_page(data.get("page_id"), current_user.id)
    if page is None:
        return jsonify({"ok": False, "error": "网页不存在或已删除"}), 400
    new_page = page_service.duplicate_page(current_user.id, page)
    return jsonify({"ok": True, "data": {"id": new_page.id, "slug": new_page.slug}})


@bp.route("/api/delete", methods=["POST"])
@login_required
def api_delete():
    """软删除页面（slug 同步释放）。"""
    data = request.get_json(silent=True) or {}
    page = page_service.get_page(data.get("page_id"), current_user.id)
    if page is None:
        return jsonify({"ok": False, "error": "网页不存在或已删除"}), 400
    page_service.soft_delete_page(page)
    return jsonify({"ok": True})
