"""健身：列表 / 统计 / 新建 / 编辑 / 删除。

页面视图 GET /fitness/；写操作均为 fetch JSON 接口（@csrf.exempt），
返回 {"ok": true, "data": ...} 或 {"ok": false, "error": "..."}（HTTP 400）。
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from app.extensions import csrf
from app.services import fitness_service

bp = Blueprint("fitness", __name__, url_prefix="/fitness")


def _record_json(rec) -> dict:
    return {
        "id": rec.id,
        "date": rec.date.isoformat(),
        "workout_type": rec.workout_type,
        "duration_min": rec.duration_min,
        "intensity": rec.intensity,
        "calories": rec.calories,
        "weight_kg": rec.weight_kg,
        "notes": rec.notes,
    }


def _get_or_error(record_id):
    try:
        record_id = int(record_id)
    except (TypeError, ValueError):
        raise ValueError("记录 ID 非法")
    rec = fitness_service.get_record(record_id, current_user.id)
    if rec is None:
        raise ValueError("记录不存在")
    return rec


@bp.route("/")
@login_required
def index():
    records = fitness_service.list_records(current_user.id)
    items = [_record_json(r) for r in records]
    return render_template(
        "fitness/index.html",
        records=items,
        stats=fitness_service.summary(records),
        workout_types=fitness_service.WORKOUT_TYPES,
        intensities=fitness_service.INTENSITIES,
    )


@bp.route("/api/create", methods=["POST"])
@csrf.exempt
@login_required
def api_create():
    data = request.get_json(silent=True) or {}
    try:
        rec = fitness_service.create_record(
            current_user.id,
            workout_date=data.get("date"),
            workout_type=data.get("workout_type"),
            duration_min=data.get("duration_min"),
            intensity=data.get("intensity"),
            calories=data.get("calories"),
            weight_kg=data.get("weight_kg"),
            notes=data.get("notes") or "",
        )
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "data": _record_json(rec)})


@bp.route("/api/update", methods=["POST"])
@csrf.exempt
@login_required
def api_update():
    data = request.get_json(silent=True) or {}
    try:
        rec = _get_or_error(data.get("record_id"))
        # 用 k in data 而非值非 None 判断：允许把可选字段（卡路里/体重）清空为 null
        fields = {k: data.get(k) for k in
                  ("date", "workout_type", "duration_min", "intensity",
                   "calories", "weight_kg", "notes") if k in data}
        rec = fitness_service.update_record(rec, **fields)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "data": _record_json(rec)})


@bp.route("/api/delete", methods=["POST"])
@csrf.exempt
@login_required
def api_delete():
    data = request.get_json(silent=True) or {}
    try:
        rec = _get_or_error(data.get("record_id"))
        fitness_service.delete_record(rec)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})
