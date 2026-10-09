"""Account discovery for clients bound with an existing per-user API Token."""
from flask import Blueprint, g, request

from app.utils.integration_api import api_authenticated, register_api_errors, success

bp = Blueprint("integration_account", __name__, url_prefix="/api/v1")
register_api_errors(bp)


@bp.get("/me")
@api_authenticated
def me():
    user = g.api_user
    return success({
        "id": user.id,
        "username": user.username,
        "timezone": user.timezone,
        "api_version": "v1",
        "capabilities": ["events", "event_occurrences", "tasks", "conversations", "chat", "sse", "websocket", "navigation_vault",
                         "storage", "anime_search", "downloads", "native_agents", "download_event_replay"],
        "websocket_path": request.script_root + "/api/v1/ws",
        "download_websocket_path": request.script_root + "/api/v1/download-events/ws",
    })
