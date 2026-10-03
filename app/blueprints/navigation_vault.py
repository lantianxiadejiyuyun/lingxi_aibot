"""Login-protected navigation vault viewer; reveal requires an explicit CSRF POST."""
from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from app.services import navigation_vault_reader
from app.utils.integration_api import ApiError, json_body, pagination, register_api_errors
from app.utils.scoping import user_scope

bp = Blueprint("navigation_vault", __name__, url_prefix="/settings/navigation-vault")
register_api_errors(bp)


@bp.after_request
def private_response(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@bp.get("/")
@login_required
def index():
    return render_template("settings/navigation_vault.html")


@bp.get("/api/items")
@login_required
def items():
    if set(request.args) - {"q", "limit", "offset"}:
        raise ApiError("仅支持 q、limit、offset 参数")
    query = request.args.get("q", "").strip()
    if len(query) > 200:
        raise ApiError("搜索内容不能超过 200 个字符")
    limit, offset = pagination()
    with user_scope(current_user.id):
        data = navigation_vault_reader.list_vault_items(current_user.id, q=query, limit=limit, offset=offset)
    # Keep the UI boundary explicit, even if a future upstream adds extra fields.
    metadata = [{key: item.get(key) for key in ("id", "title", "site", "username_masked", "password_set")}
                for item in data["items"]]
    return jsonify(ok=True, data={"items": metadata, "pagination": data["pagination"]})


@bp.post("/api/reveal")
@login_required
def reveal():
    body = json_body()
    if set(body) != {"id"}:
        raise ApiError("仅支持 id 参数")
    item_id = body.get("id")
    if (not isinstance(item_id, str) or not item_id.strip() or len(item_id) > 200
            or any(ord(character) < 32 for character in item_id)):
        raise ApiError("id 必须为 1–200 个字符的有效文本")
    with user_scope(current_user.id):
        data = navigation_vault_reader.reveal_vault_item(current_user.id, item_id)
    return jsonify(ok=True, data={key: data.get(key) for key in ("id", "title", "site", "username", "password")})
