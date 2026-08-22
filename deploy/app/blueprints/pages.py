"""网页生成器：管理（列表/在线编辑/复制/删除/显示开关）+ 公开访问（后台/网页域名分离）。

域名体系（设置页「域名设置」可配）：
- admin_domain（ADMIN_DOMAIN）：灵犀 管理后台域名，如 admin.eugenstudio.cn
- page_domain（PAGE_DOMAIN）：AI 网页域名，如 web.eugenstudio.cn
  → 公开网页地址：https://web.eugenstudio.cn/<slug>（仅路径式，无需 DNS 泛解析）

访问控制：
- 公开页面：网页域名 /<slug>（或 /p/<slug> 别名）任何人可访问
- 私有页面：仅后台域名 /p/<slug> 登录后可见（会话 cookie 绑定后台域名）
- 隐藏页面（enabled=False）：一律 404
- 后台域名守卫：配置 admin_domain 后，非后台/网页域名（且非本地开发主机）的
  请求 302 跳转到后台域名
"""
from __future__ import annotations

import re

from flask import (
    Blueprint, Response, abort, flash, jsonify, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required

from app.extensions import csrf
from app.services import page_service
from app.utils.timeutil import fmt_dt, user_tz

bp = Blueprint("pages", __name__, url_prefix="/pages")
site_bp = Blueprint("pages_site", __name__)

_LOCAL_HOST_RE = re.compile(
    r"^(localhost|127\.0\.0\.1|::1|0\.0\.0\.0|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})$"
)


def _host() -> str:
    """请求主机名（小写，IPv6 形如 [::1]:5000 时取 ::1，不带端口）。"""
    host = (request.host or "").lower()
    if host.startswith("["):
        return host.split("]", 1)[0].lstrip("[")
    return host.split(":", 1)[0]


def _render_page(page) -> Response:
    """把页面 content 作为 HTML 渲染输出。"""
    return Response(page.content or "", mimetype="text/html; charset=utf-8")


# ---------- 域名守卫 ----------

@site_bp.before_app_request
def _admin_host_guard():
    """后台域名守卫：配置 admin_domain 后，其他域名（非网页域名、非本地/内网开发）跳转到后台域名。

    数据库不可用（首次安装尚未配置）时跳过守卫，保证 /setup 安装向导可访问。
    """
    try:
        admin = page_service.admin_domain_configured()
    except Exception:  # noqa: BLE001 —— 数据库未初始化/不可用
        return None
    if not admin:
        return None
    host = _host()
    if host == admin or host == page_service.page_domain_configured():
        return None
    if _LOCAL_HOST_RE.match(host):
        return None  # 本地开发/内网直连放行（含 IPv6 回环）
    if request.method in ("GET", "HEAD"):
        return redirect(f"https://{admin}{request.full_path}", code=302)
    abort(404)


@site_bp.before_app_request
def _page_host_routing():
    """网页域名：只服务页面渲染（/<slug> 或 /p/<slug>），其余路径 404。

    仅公开页面；私有/隐藏/不存在一律 404（网页域名不提供登录能力）。
    数据库不可用（首次安装）时同样跳过，避免安装向导被拦截。
    """
    try:
        page_domain = page_service.page_domain_configured()
    except Exception:  # noqa: BLE001 —— 数据库未初始化/不可用
        page_domain = ""
    if not page_domain or _host() != page_domain:
        return None
    if request.method not in ("GET", "HEAD"):
        abort(404)
    path = request.path.strip("/")
    if path.startswith("img/"):
        return None  # /img/<文件名> 交给图片文件路由处理（公开图可访问，私有图 404）
    slug = ""
    if path.startswith("p/"):
        slug = path[2:]
    elif path and "/" not in path:
        slug = path
    if not slug:
        abort(404)
    page = page_service.visible_by_slug(slug)
    if page is None or not page.is_public:
        abort(404)
    return _render_page(page)


# ---------- 公开访问（后台域名 / 本地开发）----------

@site_bp.route("/p/<slug>")
def public_view(slug):
    """路径模式公开访问：/p/<slug>（私有页面需登录）。"""
    page = page_service.visible_by_slug(slug)
    if page is None or (not page.is_public and not current_user.is_authenticated):
        abort(404)
    return _render_page(page)


# ---------- 管理页 ----------

def _page_view(page) -> dict:
    """页面 → 模板用 dict（含按域名体系计算的访问地址）。"""
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
    pages = page_service.list_pages()
    items = [_page_view(p) for p in pages]
    return render_template(
        "pages/index.html",
        pages=items,
        page_domain=page_service.page_domain_configured(),
        admin_domain=page_service.admin_domain_configured(),
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
        row = page_service.get_page(page_id)
        if row is None:
            flash("网页不存在或已删除", "error")
            return redirect(url_for("pages.index"))
        page = _page_view(row)
    return render_template("pages/edit.html", page=page)


@bp.route("/api/save", methods=["POST"])
@csrf.exempt
@login_required
def api_save():
    """新建或更新页面（JSON）：page_id 为空新建，否则更新。"""
    data = request.get_json(silent=True) or {}
    page_id = data.get("page_id")
    try:
        if page_id in (None, ""):
            page = page_service.create_page(
                title=data.get("title") or "",
                content=data.get("content") or "",
                slug=(data.get("slug") or "").strip() or None,
                description=data.get("description") or "",
                is_public=bool(data.get("is_public", True)),
                enabled=bool(data.get("enabled", True)),
            )
        else:
            page = page_service.get_page(page_id)
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
@csrf.exempt
@login_required
def api_toggle():
    """显示/隐藏开关：enabled=false 时前台 404（后台仍可编辑）。"""
    data = request.get_json(silent=True) or {}
    page = page_service.get_page(data.get("page_id"))
    if page is None:
        return jsonify({"ok": False, "error": "网页不存在或已删除"}), 400
    page_service.update_page(page, enabled=bool(data.get("enabled", True)))
    return jsonify({"ok": True, "data": {"id": page.id, "enabled": page.enabled}})


@bp.route("/api/duplicate", methods=["POST"])
@csrf.exempt
@login_required
def api_duplicate():
    """复制页面为新页面。"""
    data = request.get_json(silent=True) or {}
    page = page_service.get_page(data.get("page_id"))
    if page is None:
        return jsonify({"ok": False, "error": "网页不存在或已删除"}), 400
    new_page = page_service.duplicate_page(page)
    return jsonify({"ok": True, "data": {"id": new_page.id, "slug": new_page.slug}})


@bp.route("/api/delete", methods=["POST"])
@csrf.exempt
@login_required
def api_delete():
    """软删除页面（slug 同步释放）。"""
    data = request.get_json(silent=True) or {}
    page = page_service.get_page(data.get("page_id"))
    if page is None:
        return jsonify({"ok": False, "error": "网页不存在或已删除"}), 400
    page_service.soft_delete_page(page)
    return jsonify({"ok": True})
