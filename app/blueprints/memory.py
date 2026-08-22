"""记忆管理：查看/删除长期记忆 + 手动触发上下文梳理。"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from app.extensions import csrf
from app.services import memory_service
from app.utils.timeutil import fmt_dt, user_tz

bp = Blueprint("memory", __name__, url_prefix="/memory")


@bp.route("/")
@login_required
def index():
    """长期记忆列表页。"""
    tz = user_tz(current_user)
    items = [
        {"id": m.id, "content": m.content,
         "source": "手动" if m.source == "manual" else "自动",
         "importance": m.importance,
         "expires": fmt_dt(m.expires_at, tz) or "",
         "updated_at": fmt_dt(m.updated_at, tz)}
        for m in memory_service.list_memories(current_user.id)
    ]
    return render_template("memory/index.html", memories=items)


@bp.route("/api/delete", methods=["POST"])
@csrf.exempt
@login_required
def api_delete():
    """软删除一条记忆。"""
    data = request.get_json(silent=True) or {}
    memory = memory_service.get_memory(data.get("memory_id"), current_user.id)
    if memory is None:
        return jsonify({"ok": False, "error": "记忆不存在或已删除"}), 400
    memory_service.soft_delete_memory(memory)
    return jsonify({"ok": True})


@bp.route("/api/consolidate", methods=["POST"])
@csrf.exempt
@login_required
def api_consolidate():
    """手动触发上下文梳理（压缩长对话 + 抽取长期记忆），耗时可能较长。"""
    report = memory_service.consolidate_all(current_user)
    return jsonify({"ok": True, "data": report})
