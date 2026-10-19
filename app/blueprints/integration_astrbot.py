"""Token-authenticated AstrBot private routing; never accept browser credentials."""
from flask import Blueprint, g

from app.services import astrbot_service
from app.utils.integration_api import ApiError, api_authenticated, json_body, register_api_errors, success

bp = Blueprint("integration_astrbot", __name__, url_prefix="/api/v1/integrations/astrbot")
register_api_errors(bp)


def _body(allowed):
    body = json_body()
    if set(body) - set(allowed):
        raise ApiError("包含未知字段")
    return body


@bp.get("/binding")
@api_authenticated
def read_binding():
    return success(astrbot_service.binding_view(g.api_user.id))


@bp.put("/binding")
@api_authenticated
def bind():
    return success(astrbot_service.bind(g.api_user.id, _body({"session"}).get("session")))


@bp.delete("/binding")
@api_authenticated
def unbind():
    session = astrbot_service._session(_body({"session"}).get("session"))
    return success(astrbot_service.unbind(g.api_user.id, expected_session=session))


@bp.post("/outbox/claim")
@api_authenticated
def claim():
    return success(astrbot_service.claim(g.api_user.id, _body({"limit"}).get("limit", 1)))


@bp.post("/outbox/<int:message_id>/ack")
@api_authenticated
def acknowledge(message_id):
    body = _body({"lease_token", "outcome"})
    return success(astrbot_service.acknowledge(g.api_user.id, message_id, body.get("lease_token"), body.get("outcome")))
