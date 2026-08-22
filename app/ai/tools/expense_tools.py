"""消费 AI 工具：查询 / 统计 / 记账 / 更新 / 删除。"""
from __future__ import annotations

from datetime import datetime

from flask_login import current_user

from app.ai.registry import register_tool
from app.models.expense import ExpenseRecord
from app.services import expense_service
from app.utils.timeutil import user_tz

_CATEGORIES = "、".join(expense_service.CATEGORIES)
_PAYMENTS = "、".join(expense_service.PAYMENT_METHODS)


def _today() -> str:
    return datetime.now(user_tz(current_user)).date().isoformat()


def _find(record_id) -> ExpenseRecord:
    try:
        rid = int(record_id)
    except (TypeError, ValueError):
        raise ValueError("record_id 必须是数字") from None
    rec = expense_service.get_record(rid, current_user.id)
    if rec is None:
        raise ValueError(f"消费记录 {rid} 不存在")
    return rec


def _summary(rec: ExpenseRecord) -> dict:
    return {
        "id": rec.id,
        "date": rec.date.isoformat(),
        "amount": rec.amount,
        "category": rec.category,
        "payment_method": rec.payment_method,
        "notes": rec.notes or "",
    }


@register_tool(
    name="list_expenses",
    description=(
        "查询消费记录。可按 start_date/end_date（YYYY-MM-DD）和 category 筛选。"
        f"分类可选：{_CATEGORIES}。"
        "适合「这个月花了哪些钱」「最近餐饮支出」。需要汇总数字时用 get_expense_stats。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "start_date": {"type": "string", "description": "开始日期 YYYY-MM-DD，可空"},
            "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD，可空"},
            "category": {"type": "string", "description": f"分类：{_CATEGORIES}，可空"},
            "limit": {"type": "integer", "description": "最多返回条数，默认 50"},
        },
        "required": [],
    },
)
def list_expenses(start_date=None, end_date=None, category=None, limit=50):
    try:
        limit = max(1, min(200, int(limit or 50)))
    except (TypeError, ValueError):
        limit = 50
    records = expense_service.list_records(
        current_user.id,
        start_date=start_date or None,
        end_date=end_date or None,
        category=category or None,
        limit=limit,
    )
    if not records:
        return "暂无消费记录。"
    return [_summary(r) for r in records]


@register_tool(
    name="get_expense_stats",
    description=(
        "消费统计：总支出、笔数、平均、分类占比、按月、按支付方式。"
        "可传 start_date/end_date 限定区间（如本月）。适合「这个月花了多少」「餐饮占比」。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "start_date": {"type": "string", "description": "开始日期 YYYY-MM-DD，可空"},
            "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD，可空"},
        },
        "required": [],
    },
)
def get_expense_stats(start_date=None, end_date=None):
    return expense_service.stats(
        current_user.id,
        start_date=start_date or None,
        end_date=end_date or None,
    )


@register_tool(
    name="create_expense",
    description=(
        f"记一笔消费。amount 金额（元）必填；category 分类：{_CATEGORIES}（默认其他）；"
        f"date 日期 YYYY-MM-DD（默认今天）；payment_method 支付方式：{_PAYMENTS}（默认其他）；"
        "notes 备注。适合「午饭 38 块微信支付」「记一笔打车 25」。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "amount": {"type": "number", "description": "金额（元），必填，非负"},
            "category": {"type": "string", "description": f"分类：{_CATEGORIES}"},
            "date": {"type": "string", "description": "日期 YYYY-MM-DD，默认今天"},
            "payment_method": {"type": "string", "description": f"支付方式：{_PAYMENTS}"},
            "notes": {"type": "string", "description": "备注，可空"},
        },
        "required": ["amount"],
    },
)
def create_expense(amount, category="其他", date=None, payment_method="其他", notes=""):
    rec = expense_service.create_record(
        current_user.id,
        amount=amount,
        category=category,
        record_date=date or _today(),
        payment_method=payment_method,
        notes=notes or "",
    )
    return f"已记账 [id={rec.id}]：{rec.date.isoformat()} {rec.category} ¥{rec.amount}"


@register_tool(
    name="update_expense",
    description="更新已有消费记录。record_id 必填；其余字段只改传入的。",
    parameters={
        "type": "object",
        "properties": {
            "record_id": {"type": "integer", "description": "记录 ID，必填"},
            "amount": {"type": "number"},
            "category": {"type": "string"},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "payment_method": {"type": "string"},
            "notes": {"type": "string"},
        },
        "required": ["record_id"],
    },
)
def update_expense(record_id, amount=None, category=None, date=None,
                   payment_method=None, notes=None):
    rec = _find(record_id)
    fields = {}
    if amount is not None:
        fields["amount"] = amount
    if category is not None:
        fields["category"] = category
    if date is not None:
        fields["date"] = date
    if payment_method is not None:
        fields["payment_method"] = payment_method
    if notes is not None:
        fields["notes"] = notes
    rec = expense_service.update_record(rec, **fields)
    return _summary(rec)


@register_tool(
    name="delete_expense",
    description="删除一条消费记录（硬删除，不可恢复）。请确认用户明确要求后再调用。",
    parameters={
        "type": "object",
        "properties": {"record_id": {"type": "integer", "description": "记录 ID，必填"}},
        "required": ["record_id"],
    },
    dangerous=True,
)
def delete_expense(record_id):
    rec = _find(record_id)
    label = f"{rec.date.isoformat()} {rec.category} ¥{rec.amount}"
    expense_service.delete_record(rec)
    return f"🗑️ 已删除消费记录 [id={record_id}]：{label}"
