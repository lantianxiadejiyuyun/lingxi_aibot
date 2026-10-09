"""AI 对话页：会话列表 / 消息历史 / 流式发送（SSE）/ 新建 / 删除 / 文本转语音。"""
from __future__ import annotations

import json

import requests
from flask import (
    Blueprint, Response, current_app, jsonify, render_template, request, stream_with_context,
)
from flask_login import current_user, login_required

from app.ai.executor import run_chat
from app.ai.llm import LLMClient
from app.extensions import db
from app.models.conversation import Conversation
from app.utils.timeutil import fmt_dt, user_tz

bp = Blueprint("chat", __name__, url_prefix="/chat")


def _json_ok(data=None):
    return jsonify({"ok": True, "data": data})


def _json_err(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


def _sse(event: str, text) -> str:
    """单条 SSE 块：event 行 + data 行（按换行拆分，前端以 \\n 重新拼接）。"""
    data = str(text).split("\n")
    return "event: " + event + "\n" + "".join("data: " + line + "\n" for line in data) + "\n"


def _owned_conversation(conv_id) -> Conversation | None:
    """取当前用户的会话；非本用户返回 None。"""
    conv = db.session.get(Conversation, conv_id)
    if conv is None or conv.user_id != current_user.id:
        return None
    return conv


@bp.route("/")
@login_required
def index():
    conversations = (Conversation.query.filter(Conversation.user_id == current_user.id)
                     .order_by(Conversation.updated_at.desc()).limit(20).all())
    from app.ai.prompts import persona_name

    return render_template(
        "chat/index.html",
        conversations=conversations,
        persona_name=persona_name(current_user),
    )


@bp.route("/api/conversations", methods=["GET"])
@login_required
def api_conversations():
    tz = user_tz(current_user)
    convs = (Conversation.query.filter(Conversation.user_id == current_user.id)
             .order_by(Conversation.updated_at.desc()).limit(20).all())
    data = [
        {"id": c.id, "title": c.title, "updated_at": fmt_dt(c.updated_at, tz)}
        for c in convs
    ]
    return _json_ok(data)


@bp.route("/api/messages/<int:conv_id>", methods=["GET"])
@login_required
def api_messages(conv_id):
    conv = _owned_conversation(conv_id)
    if conv is None:
        return _json_err("会话不存在")
    tz = user_tz(current_user)
    data = [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "tool_calls": m.tool_calls,
            "created_at": fmt_dt(m.created_at, tz),
        }
        for m in conv.messages
    ]
    return _json_ok(data)


@bp.route('/api/controls/<int:conv_id>', methods=['GET'])
@login_required
def api_controls(conv_id):
    conv = _owned_conversation(conv_id)
    if conv is None:
        return _json_err('会话不存在', 404)
    from app.services.model_control_service import chat_controls

    return _json_ok(chat_controls(conv, current_user))


@bp.route("/api/send", methods=["POST"])
@login_required
def api_send():
    from app.services.context_service import conversation_lock
    from app.services.integration_chat_service import validate_chat_payload
    from app.utils.integration_api import ApiError
    from app.utils.scoping import user_scope

    data = request.get_json(silent=True)
    # The web sidebar supplies DOM attribute IDs as strings. Keep that accepted,
    # but never silently redirect a deleted/invalid conversation into a new one.
    if isinstance(data, dict):
        data = dict(data)
        raw_id = data.get("conversation_id")
        if raw_id == "":
            data["conversation_id"] = None
        elif isinstance(raw_id, str) and raw_id.isascii() and raw_id.isdecimal() and len(raw_id) <= 10:
            data["conversation_id"] = int(raw_id)
    try:
        payload = validate_chat_payload(data)
    except ApiError as exc:
        return _json_err(str(exc), exc.status)
    message = payload["message"]
    if payload["conversation_id"] is None:
        conversation = Conversation(title="新对话", user_id=current_user.id)
        db.session.add(conversation)
        db.session.commit()
    else:
        conversation = _owned_conversation(payload["conversation_id"])
        if conversation is None:
            return _json_err("会话不存在，请重新选择或新建对话", 404)
    # 在路由内（会话仍存活）先取出纯整型 id；生成器运行时上下文已切换，
    # 对象可能已脱离会话，必须用 id 重新加载
    conv_id = conversation.id
    user_id = getattr(current_user, "id", None)

    def gen():
        # Flush response headers and expose the actual conversation immediately,
        # even when its previous turn is still holding the conversation lock.
        yield _sse("start", json.dumps({"conversation_id": conv_id}))
        with current_app.app_context(), user_scope(user_id), conversation_lock(conv_id):
            # 生成器运行在独立的应用上下文里（db.session 随上下文隔离），
            # 必须重新从库里取会话与用户，否则对象与当前会话脱离
            from app.models.user import User

            db.session.rollback()
            conv = db.session.get(Conversation, conv_id)
            if conv is None or conv.user_id != user_id:
                yield _sse("error", "会话不存在")
                yield _sse("done", "")
                return
            user = db.session.get(User, user_id) if user_id else None
            if user is None:
                yield _sse("error", "用户不存在")
                yield _sse("done", "")
                return
            iterator = None
            reply = ""
            completed = False
            failed = False
            try:
                iterator = run_chat(conv, message, user)
                for ev in iterator:
                    kind, payload = ev
                    if kind == "delta":
                        yield _sse("delta", payload)
                    elif kind == "ack":
                        # 先把「收到：…」推到气泡里，正式回答随后流式接上
                        yield _sse("delta", payload + "\n\n")
                    elif kind == "tool":
                        yield _sse("tool", json.dumps(payload, ensure_ascii=False))
                    elif kind == 'notice':
                        yield _sse('tool', json.dumps({'name': '会话控制', 'result': payload, 'ok': True}, ensure_ascii=False))
                    elif kind == "title":
                        yield _sse("title", payload)
                    elif kind == "done":
                        reply = payload
                        completed = True
                        break
                    elif kind == "error":
                        failed = True
                        yield _sse("error", payload)
                        break
                if not completed and not failed:
                    yield _sse("error", "回复意外中断，请检查会话记录后再继续")
            except Exception:  # noqa: BLE001 —— 任何异常先发 error 再 done
                db.session.rollback()
                current_app.logger.exception("网页对话执行失败")
                yield _sse("error", "对话处理中断，请检查会话记录后再继续")
            finally:
                # Closing the HTTP response must also close the executor and its
                # provider stream, releasing the lock for the next message.
                if iterator is not None and hasattr(iterator, "close"):
                    iterator.close()
            yield _sse("done", reply if completed else "")

    resp = Response(stream_with_context(gen()), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@bp.route("/api/tts", methods=["POST"])
@login_required
def api_tts():
    """文本转语音（OpenAI 兼容 /audio/speech），返回 mp3；未配置时前端降级浏览器朗读。"""
    from app.services.settings_service import get_setting_from

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return _json_err("text 不能为空")
    text = text[:1000]
    base = str(get_setting_from("tts_base_url", "TTS_BASE_URL", "") or "").strip().rstrip("/")
    key = str(get_setting_from("tts_api_key", "TTS_API_KEY", "") or "").strip()
    model = str(get_setting_from("tts_model", "TTS_MODEL", "") or "").strip()
    if not (base and key and model):
        return jsonify({"ok": False, "error": "未配置 TTS，请使用浏览器朗读"}), 400
    voice = str(get_setting_from("tts_voice", "TTS_VOICE", "alloy") or "alloy").strip()
    try:
        resp = requests.post(
            f"{base}/audio/speech",
            json={"model": model, "input": text, "voice": voice, "response_format": "mp3"},
            headers={"Authorization": f"Bearer {key}"},
            timeout=60,
        )
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 502
    if resp.status_code != 200:
        return jsonify({"ok": False, "error": f"TTS 失败 HTTP {resp.status_code}"}), 502
    audio = Response(resp.content, mimetype="audio/mpeg")
    audio.headers["Cache-Control"] = "no-store"
    return audio


@bp.route("/api/image", methods=["POST"])
@login_required
def api_image():
    from app.services.image_input_service import ImageInputError, MAX_IMAGE_BYTES, receive_image

    if request.content_length and request.content_length > MAX_IMAGE_BYTES + 65536:
        return _json_err("图片不能超过 8 MiB", 413)
    raw_id = request.form.get("conversation_id")
    conversation = None
    if raw_id not in (None, ""):
        try:
            conversation = _owned_conversation(int(raw_id))
        except (ValueError, TypeError, OverflowError):
            conversation = None
        if conversation is None:
            return _json_err("会话不存在", 404)
    upload = request.files.get("file")
    if upload is None:
        return _json_err("请选择一张图片")
    data = upload.read(MAX_IMAGE_BYTES + 1)
    try:
        result = receive_image(current_user, data, conversation,
                               prompt=request.form.get("prompt", ""))
    except ImageInputError as e:
        return _json_err(str(e), e.status_code)
    except Exception:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception("网页收图失败")
        return _json_err("图片处理失败，请稍后重试", 500)
    return _json_ok(result)


@bp.route("/api/new", methods=["POST"])
@login_required
def api_new():
    conv = Conversation(title="新对话", user_id=current_user.id)
    db.session.add(conv)
    db.session.commit()
    return _json_ok({"id": conv.id})


@bp.route("/api/delete/<int:conv_id>", methods=["POST"])
@login_required
def api_delete(conv_id):
    from app.models.setting import Setting
    from app.services.context_service import conversation_lock

    user_id = current_user.id
    with conversation_lock(conv_id):
        db.session.rollback()
        conv = _owned_conversation(conv_id)
        if conv is None:
            return _json_err("会话不存在")
        Setting.query.filter(Setting.user_id == user_id, Setting.key.in_(
            [f"chat_llm:{conv_id}", f"chat_context:{conv_id}"])).delete(synchronize_session=False)
        db.session.delete(conv)
        db.session.commit()
    return _json_ok({"id": conv_id})


@bp.route("/api/clear-all", methods=["POST"])
@login_required
def api_clear_all():
    """清空当前用户所有会话与消息（含简报/报告等系统会话），数据库级 CASCADE 删除 messages。"""
    from app.models.setting import Setting
    from app.services.context_service import all_conversations_lock

    user_id = current_user.id
    with all_conversations_lock():
        db.session.rollback()
        conversations = Conversation.query.filter_by(user_id=user_id).all()
        count = len(conversations)
        # ORM cascade also works on SQLite test/development databases where
        # foreign-key cascades may be disabled.
        for conversation in conversations:
            db.session.delete(conversation)
        (Setting.query.filter(Setting.user_id == user_id,
                              Setting.key.startswith("chat_llm:", autoescape=True)
                              | Setting.key.startswith("chat_context:", autoescape=True))
         .delete(synchronize_session=False))
        db.session.commit()
    return _json_ok({"deleted": count})
