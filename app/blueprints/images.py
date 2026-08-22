"""图片库：管理页 / 生成 / 改图 / 公开开关 / 删除 + /img/ 文件访问。

- 文件访问 /img/<filename>：文件名 uuid 化不可枚举；公开图片任何人可访问
  （供生成的网页嵌入），私有图片仅登录用户可见（未登录 404，不泄露存在性）
- 生成/改图耗时较长（10-60s+），接口为同步 JSON，前端需给 loading 提示
"""
from __future__ import annotations

import re

from flask import (
    Blueprint, abort, current_app, jsonify, render_template, request, send_file,
)
from flask_login import current_user, login_required

from app.extensions import csrf
from app.services import image_service
from app.utils.timeutil import fmt_dt, user_tz

bp = Blueprint("images", __name__, url_prefix="/images")
img_bp = Blueprint("image_files", __name__)

_FILENAME_RE = re.compile(r"^img-[a-f0-9]{8}-\d{1,6}\.(png|jpg|jpeg|webp|gif)$")

_COMMON_SIZES = ("1024x1024", "720x1280", "1280x720", "512x512", "1536x1024", "1024x1536")


# ---------- 文件访问 ----------

@img_bp.route("/img/<path:filename>")
def file_view(filename):
    """图片文件访问：严格文件名白名单 + DB 记录校验，防路径穿越与枚举。"""
    filename = filename.replace("\\", "/").split("/")[-1]
    if not _FILENAME_RE.match(filename):
        abort(404)
    asset = image_service.get_by_filename(filename)
    if asset is None:
        abort(404)
    if not asset.is_public and not current_user.is_authenticated:
        abort(404)
    path = current_app.config["IMAGE_DIR"] / filename
    if not path.exists():
        abort(404)
    resp = send_file(path, conditional=True)
    if asset.is_public:
        # 公开图：短缓存 + 条件请求（ETag），转为私有后不会长期滞留 CDN/浏览器缓存
        resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    else:
        resp.headers["Cache-Control"] = "private, max-age=0"
    return resp


# ---------- 管理页 ----------

def _asset_view(asset) -> dict:
    tz = user_tz(current_user)
    return {
        "id": asset.id,
        "prompt": asset.prompt,
        "instruction": asset.instruction,
        "model": asset.model,
        "size": asset.size,
        "url": image_service.asset_url(asset),
        "is_public": asset.is_public,
        "parent_id": asset.parent_id,
        "created_at": fmt_dt(asset.created_at, tz),
    }


@bp.route("/")
@login_required
def index():
    """图片库页面。"""
    cfg = image_service._read_config()
    configured = image_service.is_configured()
    items = [_asset_view(a) for a in image_service.list_images(current_user.id)]
    return render_template(
        "images/index.html",
        images=items,
        configured=configured,
        default_size=cfg["size"],
        sizes=_COMMON_SIZES,
    )


@bp.route("/api/generate", methods=["POST"])
@csrf.exempt
@login_required
def api_generate():
    """生成图片（同步，耗时较长）。"""
    data = request.get_json(silent=True) or {}
    try:
        asset = image_service.generate(current_user.id, data.get("prompt") or "",
                                       size=data.get("size") or None)
    except image_service.ImageError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "data": _asset_view(asset)})


@bp.route("/api/edit", methods=["POST"])
@csrf.exempt
@login_required
def api_edit():
    """AI 改图：编辑接口或降级重生成，产出新图。"""
    data = request.get_json(silent=True) or {}
    asset = image_service.get_image(data.get("image_id"), current_user.id)
    if asset is None:
        return jsonify({"ok": False, "error": "图片不存在或已删除"}), 400
    try:
        new_asset = image_service.edit(asset, data.get("instruction") or "")
    except image_service.ImageError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "data": _asset_view(new_asset)})


@bp.route("/api/toggle-public", methods=["POST"])
@csrf.exempt
@login_required
def api_toggle_public():
    """公开/私有开关。"""
    data = request.get_json(silent=True) or {}
    asset = image_service.get_image(data.get("image_id"), current_user.id)
    if asset is None:
        return jsonify({"ok": False, "error": "图片不存在或已删除"}), 400
    image_service.set_public(asset, bool(data.get("is_public", False)))
    return jsonify({"ok": True, "data": {"id": asset.id, "is_public": asset.is_public}})


@bp.route("/api/delete", methods=["POST"])
@csrf.exempt
@login_required
def api_delete():
    """软删除图片。"""
    data = request.get_json(silent=True) or {}
    asset = image_service.get_image(data.get("image_id"), current_user.id)
    if asset is None:
        return jsonify({"ok": False, "error": "图片不存在或已删除"}), 400
    image_service.soft_delete(asset)
    return jsonify({"ok": True})
