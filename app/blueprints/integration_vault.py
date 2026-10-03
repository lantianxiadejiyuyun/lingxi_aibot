"""Token-bound vault registration, metadata listing and explicit single reveal."""
from flask import Blueprint, g, request

from app.services.navigation_vault_service import (
    navigation_vault_status, register_navigation_vault, unregister_navigation_vault,
)
from app.services.navigation_vault_reader import list_vault_items, reveal_vault_item
from app.utils.integration_api import (
    ApiError, api_authenticated, json_body, pagination, register_api_errors, success,
)

bp = Blueprint("integration_vault", __name__, url_prefix="/api/v1/integrations")
register_api_errors(bp)


@bp.get("/navigation-vault")
@api_authenticated
def status():
    return success(navigation_vault_status(g.api_user.id))


@bp.put("/navigation-vault")
@api_authenticated
def register():
    return success(register_navigation_vault(g.api_user.id, json_body()))


@bp.delete("/navigation-vault")
@api_authenticated
def unregister():
    return success(unregister_navigation_vault(g.api_user.id))


@bp.get("/navigation-vault/items")
@api_authenticated
def list_items():
    limit, offset = pagination()
    result = list_vault_items(g.api_user.id, q=request.args.get("q", ""), limit=limit, offset=offset,
                             grant_hash=request.headers.get("X-Navigation-Vault-Grant"))
    return success(result["items"], pagination=result["pagination"])


@bp.post("/navigation-vault/reveal")
@api_authenticated
def reveal():
    body = json_body()
    if set(body) != {"id"}:
        raise ApiError("必须且只能提供 id")
    return success(reveal_vault_item(g.api_user.id, body["id"],
                                    grant_hash=request.headers.get("X-Navigation-Vault-Grant")))
