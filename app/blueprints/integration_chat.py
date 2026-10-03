"""Token-authenticated conversations, message history and SSE chat."""
from __future__ import annotations

import json

from flask import Blueprint, Response, g, jsonify, stream_with_context

from app.extensions import db
from app.models.conversation import Conversation, Message
from app.models.setting import Setting
from app.services.context_service import conversation_lock
from app.services.integration_chat_service import (
    create_conversation, get_conversation, iter_chat_events, validate_chat_payload, validate_title,
)
from app.utils.integration_api import (
    ApiError, api_authenticated, iso_datetime, json_body, pagination, register_api_errors, success,
)
from app.utils.timeutil import utcnow

bp = Blueprint("integration_chat", __name__, url_prefix="/api/v1")
register_api_errors(bp)


def conversation_view(conversation):
    return {"id": conversation.id, "title": conversation.title,
            "created_at": iso_datetime(conversation.created_at),
            "updated_at": iso_datetime(conversation.updated_at)}


@bp.get("/conversations")
@api_authenticated
def list_conversations():
    limit, offset = pagination()
    query = Conversation.query.filter_by(user_id=g.api_user.id)
    total = query.count()
    items = query.order_by(Conversation.updated_at.desc(), Conversation.id.desc()).offset(offset).limit(limit)
    return success([conversation_view(item) for item in items],
                   pagination={"total": total, "limit": limit, "offset": offset})


@bp.post("/conversations")
@api_authenticated
def new_conversation():
    body = json_body()
    conversation = create_conversation(g.api_user.id, body.get("title", "新对话"))
    return success(conversation_view(conversation), status=201)


@bp.get("/conversations/<int:conversation_id>")
@api_authenticated
def read_conversation(conversation_id):
    return success(conversation_view(get_conversation(g.api_user.id, conversation_id)))


@bp.patch("/conversations/<int:conversation_id>")
@api_authenticated
def update_conversation(conversation_id):
    body = json_body()
    if set(body) != {"title"}:
        raise ApiError("仅支持修改 title")
    title = validate_title(body["title"])
    user_id = g.api_user.id
    with conversation_lock(conversation_id):
        db.session.rollback()
        conversation = get_conversation(user_id, conversation_id)
        conversation.title = title
        conversation.updated_at = utcnow()
        db.session.commit()
        return success(conversation_view(conversation))


@bp.delete("/conversations/<int:conversation_id>")
@api_authenticated
def delete_conversation(conversation_id):
    user_id = g.api_user.id
    with conversation_lock(conversation_id):
        db.session.rollback()
        conversation = get_conversation(user_id, conversation_id)
        Setting.query.filter(Setting.user_id == user_id, Setting.key.in_(
            [f"chat_llm:{conversation_id}", f"chat_context:{conversation_id}"])).delete(synchronize_session=False)
        db.session.delete(conversation)
        db.session.commit()
    return success({"id": conversation_id, "deleted": True})


@bp.get("/conversations/<int:conversation_id>/messages")
@api_authenticated
def list_messages(conversation_id):
    get_conversation(g.api_user.id, conversation_id)
    limit, offset = pagination()
    query = Message.query.filter_by(conversation_id=conversation_id)
    total = query.count()
    items = query.order_by(Message.id).offset(offset).limit(limit)
    return success([{"id": item.id, "role": item.role, "content": item.content,
                     "tool_calls": item.tool_calls, "created_at": iso_datetime(item.created_at)}
                    for item in items], pagination={"total": total, "limit": limit, "offset": offset})


def _send(body, conversation_id=None):
    payload = validate_chat_payload(body)
    if conversation_id is not None:
        if payload["conversation_id"] is not None and payload["conversation_id"] != conversation_id:
            raise ApiError("conversation_id 与请求路径不一致")
        payload["conversation_id"] = conversation_id
    user_id = g.api_user.id
    if payload["conversation_id"] is None:
        conversation = create_conversation(user_id)
    else:
        conversation = get_conversation(user_id, payload["conversation_id"])
    conversation_id = conversation.id
    events = iter_chat_events(conversation_id, user_id, payload["message"], payload["request_id"])
    if payload["stream"]:
        def generate():
            try:
                for event in events:
                    yield "event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            finally:
                events.close()

        response = Response(stream_with_context(generate()), mimetype="text/event-stream")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Accel-Buffering"] = "no"
        return response
    collected = list(events)
    failure = next((event for event in collected if event["type"] == "error"), None)
    data = {"conversation_id": conversation_id, "reply": collected[-1]["data"], "events": collected}
    if failure:
        return jsonify({"ok": False, "error": failure["data"], "code": "chat_failed", "data": data}), 502
    return success(data)


@bp.post("/conversations/<int:conversation_id>/messages")
@api_authenticated
def send_message(conversation_id):
    return _send(json_body(), conversation_id)


@bp.post("/chat")
@api_authenticated
def chat():
    return _send(json_body())
