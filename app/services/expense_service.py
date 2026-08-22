"""消费服务：记录增删改查 + 统计汇总（按分类 / 按月 / 按支付方式，SQL 聚合）。"""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import func

from app.extensions import db
from app.models.expense import ExpenseRecord

CATEGORIES = ["餐饮", "交通", "购物", "娱乐", "居住", "医疗", "教育", "旅行", "其他"]
PAYMENT_METHODS = ["微信", "支付宝", "现金", "银行卡", "其他"]


def parse_date(value) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("日期格式不正确（应为 YYYY-MM-DD）") from None


def _to_float(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


# ---------- CRUD ----------

def list_records(user_id: int, start_date=None, end_date=None, category=None,
                 limit: int = 500) -> list[ExpenseRecord]:
    """某用户按日期倒序列出记录，支持按日期区间 / 分类过滤。"""
    q = ExpenseRecord.query.filter(ExpenseRecord.user_id == user_id)
    if start_date:
        q = q.filter(ExpenseRecord.date >= parse_date(start_date))
    if end_date:
        q = q.filter(ExpenseRecord.date <= parse_date(end_date))
    if category:
        q = q.filter(ExpenseRecord.category == category)
    return q.order_by(ExpenseRecord.date.desc(), ExpenseRecord.id.desc()).limit(limit).all()


def get_record(record_id, user_id: Optional[int] = None) -> Optional[ExpenseRecord]:
    rec = db.session.get(ExpenseRecord, record_id)
    if rec is not None and user_id is not None and rec.user_id != user_id:
        return None
    return rec


def create_record(user_id: int, amount, category, record_date,
                  payment_method="其他", notes="") -> ExpenseRecord:
    amt = _to_float(amount)
    if amt is None or amt < 0:
        raise ValueError("金额不合法（应为非负数字）")
    rec = ExpenseRecord(
        user_id=user_id,
        amount=amt,
        category=(category or "其他").strip() or "其他",
        date=parse_date(record_date),
        payment_method=(payment_method or "其他").strip() or "其他",
        notes=notes or "",
    )
    db.session.add(rec)
    db.session.commit()
    return rec


def update_record(rec: ExpenseRecord, **fields) -> ExpenseRecord:
    """更新记录；值为 None 的字段不修改。"""
    if "amount" in fields and fields["amount"] is not None:
        amt = _to_float(fields["amount"])
        if amt is None or amt < 0:
            raise ValueError("金额不合法（应为非负数字）")
        rec.amount = amt
    if "category" in fields and fields["category"] is not None:
        rec.category = str(fields["category"]).strip() or "其他"
    if "date" in fields and fields["date"] is not None:
        rec.date = parse_date(fields["date"])
    if "payment_method" in fields and fields["payment_method"] is not None:
        rec.payment_method = str(fields["payment_method"]).strip() or "其他"
    if "notes" in fields and fields["notes"] is not None:
        rec.notes = str(fields["notes"])
    db.session.commit()
    return rec


def delete_record(rec: ExpenseRecord) -> None:
    db.session.delete(rec)
    db.session.commit()


# ---------- 统计（SQL 聚合） ----------

def stats(user_id: int, start_date=None, end_date=None) -> dict:
    """汇总：总支出 / 笔数 / 平均 / 分类占比 / 按月 / 按支付方式（数据库聚合，不拉全量）。"""
    q = ExpenseRecord.query.filter(ExpenseRecord.user_id == user_id)
    if start_date:
        q = q.filter(ExpenseRecord.date >= parse_date(start_date))
    if end_date:
        q = q.filter(ExpenseRecord.date <= parse_date(end_date))

    total, count = q.with_entities(
        func.coalesce(func.sum(ExpenseRecord.amount), 0.0),
        func.count(ExpenseRecord.id),
    ).first()
    total = round(float(total or 0), 2)
    count = int(count or 0)

    by_category = {
        c: round(float(v), 2)
        for c, v in q.with_entities(ExpenseRecord.category, func.sum(ExpenseRecord.amount))
        .group_by(ExpenseRecord.category).all()
    }
    by_payment = {
        p: round(float(v), 2)
        for p, v in q.with_entities(ExpenseRecord.payment_method, func.sum(ExpenseRecord.amount))
        .group_by(ExpenseRecord.payment_method).all()
    }
    by_month = {
        m: round(float(v), 2)
        for m, v in q.with_entities(
            func.date_format(ExpenseRecord.date, "%Y-%m"), func.sum(ExpenseRecord.amount)
        ).group_by(func.date_format(ExpenseRecord.date, "%Y-%m")).all()
    }

    return {
        "total": total,
        "count": count,
        "avg": round(total / count, 2) if count else 0,
        "by_category": by_category,
        "by_month": dict(sorted(by_month.items())),
        "by_payment": by_payment,
    }
