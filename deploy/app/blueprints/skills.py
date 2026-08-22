"""技能管理：列表 / 启用停用 / 删除 / 查看代码（技能由 AI 助手创建）。"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from app.extensions import csrf
from app.services import skill_service
from app.utils.timeutil import fmt_dt, user_tz

bp = Blueprint("skills", __name__, url_prefix="/skills")


def _view(skill) -> dict:
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "parameters": skill.parameters,
        "code": skill.code,
        "enabled": skill.enabled,
        "created_at": fmt_dt(skill.created_at, user_tz(current_user)),
        "updated_at": fmt_dt(skill.updated_at, user_tz(current_user)),
    }


@bp.route("/")
@login_required
def index():
    """技能列表页（人工审核/启用/停用/删除）。"""
    items = [_view(s) for s in skill_service.list_skills()]
    return render_template("skills/index.html", skills=items)


@bp.route("/api/toggle", methods=["POST"])
@csrf.exempt
@login_required
def api_toggle():
    """启用/停用（启用即编译并注册执行）。"""
    data = request.get_json(silent=True) or {}
    skill = skill_service.get_skill(data.get("skill_id"))
    if skill is None:
        return jsonify({"ok": False, "error": "技能不存在或已删除"}), 400
    try:
        skill_service.set_enabled(skill, bool(data.get("enabled", False)))
    except skill_service.SkillError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "data": {"id": skill.id, "enabled": skill.enabled}})


@bp.route("/api/delete", methods=["POST"])
@csrf.exempt
@login_required
def api_delete():
    """软删除技能。"""
    data = request.get_json(silent=True) or {}
    skill = skill_service.get_skill(data.get("skill_id"))
    if skill is None:
        return jsonify({"ok": False, "error": "技能不存在或已删除"}), 400
    skill_service.soft_delete(skill)
    return jsonify({"ok": True})
