"""消费：列表 / 统计看板 / 新建 / 编辑 / 删除（网页端，session 登录）。"""
from __future__ import annotations

import calendar

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from app.extensions import csrf
from app.services import expense_service

bp = Blueprint("expenses", __name__, url_prefix="/expenses")


def _record_json(rec) -> dict:
    return {
        "id": rec.id,
        "amount": rec.amount,
        "category": rec.category,
        "date": rec.date.isoformat(),
        "payment_method": rec.payment_method,
        "notes": rec.notes,
    }


def _get_or_error(record_id):
    try:
        record_id = int(record_id)
    except (TypeError, ValueError):
        raise ValueError("记录 ID 非法")
    rec = expense_service.get_record(record_id, current_user.id)
    if rec is None:
        raise ValueError("记录不存在")
    return rec


@bp.route("/")
@login_required
def index():
    month = (request.args.get("month") or "").strip()
    start = end = None
    if month:
        try:
            year, mon = (int(x) for x in month.split("-", 1))
            last_day = calendar.monthrange(year, mon)[1]
        except (TypeError, ValueError):
            month = ""
        else:
            start = f"{month}-01"
            end = f"{month}-{last_day:02d}"
    records = expense_service.list_records(current_user.id, start_date=start, end_date=end)
    return render_template(
        "expenses/index.html",
        records=[_record_json(r) for r in records],
        stats=expense_service.stats(current_user.id, start_date=start, end_date=end),
        categories=expense_service.CATEGORIES,
        payment_methods=expense_service.PAYMENT_METHODS,
        month=month,
    )


@bp.route("/api/create", methods=["POST"])
@csrf.exempt
@login_required
def api_create():
    data = request.get_json(silent=True) or {}
    try:
        rec = expense_service.create_record(
            current_user.id,
            amount=data.get("amount"),
            category=data.get("category"),
            record_date=data.get("date"),
            payment_method=data.get("payment_method"),
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
        fields = {k: data.get(k) for k in
                  ("amount", "category", "date", "payment_method", "notes") if k in data}
        rec = expense_service.update_record(rec, **fields)
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
        expense_service.delete_record(rec)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})
