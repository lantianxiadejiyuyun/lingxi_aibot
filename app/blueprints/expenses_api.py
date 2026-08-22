"""消费统计 —— App 对接 REST API（/api/v1/expenses，API Token 鉴权）。

鉴权：请求头 X-API-Token: <token> 或 Authorization: Bearer <token>。
Token 优先匹配 users.api_token（设置页「账号」生成，数据记到该用户）；
否则回退 .env API_TOKEN（记到第一个管理员，兼容旧配置）。
统一返回 {"ok": true, "data": ...} 或 {"ok": false, "error": "..."}。
"""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from app.extensions import csrf
from app.services import expense_service
from app.utils.api_auth import require_api_token

api_bp = Blueprint("expenses_api", __name__, url_prefix="/api/v1/expenses")


def _api_user():
    """App API 归属用户：由 require_api_token 解析后挂在 g.api_user。"""
    return getattr(g, "api_user", None)


def _record_json(rec) -> dict:
    return {
        "id": rec.id,
        "amount": rec.amount,
        "category": rec.category,
        "date": rec.date.isoformat(),
        "payment_method": rec.payment_method,
        "notes": rec.notes,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
    }


def _get_or_404(record_id):
    try:
        record_id = int(record_id)
    except (TypeError, ValueError):
        return None
    return expense_service.get_record(record_id)


@api_bp.route("", methods=["GET"])
@csrf.exempt
@require_api_token
def list_records():
    """列表：?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&category=餐饮&limit=100"""
    user = _api_user()
    if user is None:
        return jsonify({"ok": False, "error": "尚无用户"}), 400
    try:
        limit = int(request.args.get("limit", 500))
    except (TypeError, ValueError):
        limit = 500
    records = expense_service.list_records(
        user.id,
        start_date=request.args.get("start_date"),
        end_date=request.args.get("end_date"),
        category=request.args.get("category"),
        limit=limit,
    )
    return jsonify({"ok": True, "data": [_record_json(r) for r in records]})


@api_bp.route("/stats", methods=["GET"])
@csrf.exempt
@require_api_token
def stats():
    """统计：?start_date=&end_date=（可选日期区间）"""
    user = _api_user()
    if user is None:
        return jsonify({"ok": False, "error": "尚无用户"}), 400
    return jsonify({"ok": True, "data": expense_service.stats(
        user.id,
        start_date=request.args.get("start_date"),
        end_date=request.args.get("end_date"),
    )})


@api_bp.route("/<int:record_id>", methods=["GET"])
@csrf.exempt
@require_api_token
def get_record(record_id):
    user = _api_user()
    rec = expense_service.get_record(record_id, user.id if user else None)
    if rec is None:
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    return jsonify({"ok": True, "data": _record_json(rec)})


@api_bp.route("", methods=["POST"])
@csrf.exempt
@require_api_token
def create_record():
    user = _api_user()
    if user is None:
        return jsonify({"ok": False, "error": "尚无用户"}), 400
    data = request.get_json(silent=True) or {}
    try:
        rec = expense_service.create_record(
            user.id,
            amount=data.get("amount"),
            category=data.get("category"),
            record_date=data.get("date"),
            payment_method=data.get("payment_method"),
            notes=data.get("notes") or "",
        )
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "data": _record_json(rec)}), 201


@api_bp.route("/<int:record_id>", methods=["PUT", "PATCH"])
@csrf.exempt
@require_api_token
def update_record(record_id):
    user = _api_user()
    rec = expense_service.get_record(record_id, user.id if user else None)
    if rec is None:
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    data = request.get_json(silent=True) or {}
    try:
        fields = {k: data.get(k) for k in
                  ("amount", "category", "date", "payment_method", "notes") if k in data}
        rec = expense_service.update_record(rec, **fields)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "data": _record_json(rec)})


@api_bp.route("/<int:record_id>", methods=["DELETE"])
@csrf.exempt
@require_api_token
def delete_record(record_id):
    user = _api_user()
    rec = expense_service.get_record(record_id, user.id if user else None)
    if rec is None:
        return jsonify({"ok": False, "error": "记录不存在"}), 404
    expense_service.delete_record(rec)
    return jsonify({"ok": True})
