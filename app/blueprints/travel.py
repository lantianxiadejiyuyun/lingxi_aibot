"""出行：列表 / 新建 / 编辑 / 删除 / AI 生成行程（含多段交通与备选方案）。

页面视图 GET /travel/；写操作均为 fetch JSON 接口（@csrf.exempt），
返回 {"ok": true, "data": ...} 或 {"ok": false, "error": "..."}（HTTP 400）。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from app.extensions import csrf, db
from app.services import travel_service

bp = Blueprint("travel", __name__, url_prefix="/travel")


def _plan_json(plan) -> dict:
    return {
        "id": plan.id,
        "destination": plan.destination,
        "start_date": plan.start_date.isoformat(),
        "end_date": plan.end_date.isoformat(),
        "transports": list(plan.transports or []),
        "segments": list(plan.segments or []),
        "alternatives": list(plan.alternatives or []),
        "budget": plan.budget,
        "companions": plan.companions,
        "notes": plan.notes,
        "itinerary": plan.itinerary,
    }


def _get_or_error(plan_id):
    try:
        plan_id = int(plan_id)
    except (TypeError, ValueError):
        raise ValueError("计划 ID 非法")
    plan = travel_service.get_plan(plan_id, current_user.id)
    if plan is None:
        raise ValueError("计划不存在")
    return plan


@bp.route("/")
@login_required
def index():
    plans = travel_service.list_plans(current_user.id)
    return render_template(
        "travel/index.html",
        plans=[_plan_json(p) for p in plans],
        transport_options=travel_service.TRANSPORT_OPTIONS,
    )


@bp.route("/api/create", methods=["POST"])
@csrf.exempt
@login_required
def api_create():
    data = request.get_json(silent=True) or {}
    try:
        plan = travel_service.create_plan(
            current_user.id,
            destination=data.get("destination"),
            start_date=data.get("start_date"),
            end_date=data.get("end_date"),
            transports=data.get("transports"),
            segments=data.get("segments"),
            alternatives=data.get("alternatives"),
            budget=data.get("budget"),
            companions=data.get("companions"),
            notes=data.get("notes") or "",
        )
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "data": _plan_json(plan)})


@bp.route("/api/update", methods=["POST"])
@csrf.exempt
@login_required
def api_update():
    data = request.get_json(silent=True) or {}
    try:
        plan = _get_or_error(data.get("plan_id"))
        fields = {k: data.get(k) for k in
                  ("destination", "start_date", "end_date", "transports",
                   "segments", "alternatives", "budget", "companions", "notes")
                  if k in data}
        plan = travel_service.update_plan(plan, **fields)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "data": _plan_json(plan)})


@bp.route("/api/delete", methods=["POST"])
@csrf.exempt
@login_required
def api_delete():
    data = request.get_json(silent=True) or {}
    try:
        plan = _get_or_error(data.get("plan_id"))
        travel_service.delete_plan(plan)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@bp.route("/api/generate-itinerary", methods=["POST"])
@csrf.exempt
@login_required
def api_generate_itinerary():
    """AI 生成行程并保存到计划。"""
    data = request.get_json(silent=True) or {}
    try:
        plan = _get_or_error(data.get("plan_id"))
        content = travel_service.generate_itinerary(plan, current_user)
        plan.itinerary = content
        db.session.commit()
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "data": {"itinerary": content}})
