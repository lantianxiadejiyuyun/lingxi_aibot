"""笔记：列表 / 搜索 / 新建 / 编辑 / 删除（软删除）。

页面视图 GET /notes/；写操作均为 fetch JSON 接口（@csrf.exempt），
返回 {"ok": true, "data": ...} 或 {"ok": false, "error": "..."}（HTTP 400）。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from app.extensions import csrf, db
from app.models.note import Note
from app.services import note_service
from app.utils.timeutil import fmt_dt, user_tz

bp = Blueprint("notes", __name__, url_prefix="/notes")


def _note_json(note: Note, tz) -> dict:
    """笔记 → 前端 JSON（时间用 fmt_dt 转为用户时区字符串）。"""
    return {
        "id": note.id,
        "title": note.title,
        "content": note.content,
        "tags": list(note.tags or []),
        "created_at": fmt_dt(note.created_at, tz),
        "updated_at": fmt_dt(note.updated_at, tz),
    }


def _get_note_or_error(note_id) -> Note:
    """按 id 取未删除的笔记，非法/不存在抛 ValueError。"""
    try:
        note_id = int(note_id)
    except (TypeError, ValueError):
        raise ValueError("笔记 ID 非法")
    note = db.session.get(Note, note_id)
    if note is None or note.deleted_at is not None:
        raise ValueError("笔记不存在")
    return note


@bp.route("/")
@login_required
def index():
    """笔记列表页：顶部搜索 + 网格 + 新建/编辑弹窗。"""
    q = (request.args.get("q") or "").strip()
    tz = user_tz(current_user)
    notes = note_service.list_notes(q=q or None)
    items = [
        {
            "id": n.id,
            "title": n.title,
            "content": n.content,
            "tags": list(n.tags or []),
            "updated_at": fmt_dt(n.updated_at, tz),
        }
        for n in notes
    ]
    return render_template("notes/index.html", notes=items, q=q, tz_name=str(tz))


@bp.route("/api/create", methods=["POST"])
@csrf.exempt
@login_required
def api_create():
    """新建笔记：title 必填，content / tags（逗号分隔）可选。"""
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "标题不能为空"}), 400
    try:
        note = note_service.create_note(
            title=title,
            content=data.get("content") or "",
            tags=data.get("tags"),
        )
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "data": _note_json(note, user_tz(current_user))})


@bp.route("/api/update", methods=["POST"])
@csrf.exempt
@login_required
def api_update():
    """更新笔记：note_id 必填，title / content / tags 传了才改（None 不改）。"""
    data = request.get_json(silent=True) or {}
    try:
        note = _get_note_or_error(data.get("note_id"))
        fields = {k: data.get(k) for k in ("title", "content", "tags") if data.get(k) is not None}
        note = note_service.update_note(note, **fields)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "data": _note_json(note, user_tz(current_user))})


@bp.route("/api/delete", methods=["POST"])
@csrf.exempt
@login_required
def api_delete():
    """软删除笔记。"""
    data = request.get_json(silent=True) or {}
    try:
        note = _get_note_or_error(data.get("note_id"))
        note_service.soft_delete_note(note)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})
