"""健身 AI 工具：查询 / 记录 / 更新 / 删除 / 周分析。"""
from __future__ import annotations

from datetime import datetime, timedelta

from flask_login import current_user

from app.ai.registry import register_tool
from app.models.fitness import FitnessRecord
from app.services import fitness_service
from app.utils.timeutil import user_tz

_TYPES = "、".join(fitness_service.WORKOUT_TYPES)
_INTENSITIES = "、".join(fitness_service.INTENSITIES)


def _today() -> str:
    return datetime.now(user_tz(current_user)).date().isoformat()


def _find(record_id) -> FitnessRecord:
    try:
        rid = int(record_id)
    except (TypeError, ValueError):
        raise ValueError("record_id 必须是数字") from None
    rec = fitness_service.get_record(rid, current_user.id)
    if rec is None:
        raise ValueError(f"健身记录 {rid} 不存在")
    return rec


def _summary(rec: FitnessRecord) -> dict:
    return {
        "id": rec.id,
        "date": rec.date.isoformat(),
        "workout_type": rec.workout_type,
        "duration_min": rec.duration_min,
        "intensity": rec.intensity,
        "calories": rec.calories,
        "weight_kg": rec.weight_kg,
        "notes": rec.notes or "",
    }


@register_tool(
    name="list_fitness_records",
    description=(
        "查询健身训练记录。不传日期时返回最近记录；可按 start_date/end_date（YYYY-MM-DD）筛选。"
        "同时返回统计：次数、总时长、总卡路里、类型分布、最近体重。"
        "适合回答「最近练了什么」「这周健身情况」类问题。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "start_date": {"type": "string", "description": "开始日期 YYYY-MM-DD，可空"},
            "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD，可空"},
            "limit": {"type": "integer", "description": "最多返回条数，默认 30"},
        },
        "required": [],
    },
)
def list_fitness_records(start_date=None, end_date=None, limit=30):
    try:
        limit = max(1, min(100, int(limit or 30)))
    except (TypeError, ValueError):
        limit = 30
    records = fitness_service.list_records(current_user.id, limit=200)
    if start_date:
        start = fitness_service.parse_date(start_date)
        records = [r for r in records if r.date >= start]
    if end_date:
        end = fitness_service.parse_date(end_date)
        records = [r for r in records if r.date <= end]
    sliced = records[:limit]
    return {
        "stats": fitness_service.summary(records),
        "count": len(sliced),
        "records": [_summary(r) for r in sliced],
    }


@register_tool(
    name="create_fitness_record",
    description=(
        f"记录一次训练。date 为训练日期（YYYY-MM-DD，默认今天）；"
        f"workout_type 类型：{_TYPES}；duration_min 时长（分钟）；"
        f"intensity 强度：{_INTENSITIES}（默认中）；calories / weight_kg / notes 可选。"
        "适合「今天跑步 40 分钟」「记一笔力量训练」。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "训练日期 YYYY-MM-DD，默认今天"},
            "workout_type": {"type": "string", "description": f"类型：{_TYPES}"},
            "duration_min": {"type": "integer", "description": "时长（分钟），必填"},
            "intensity": {"type": "string", "enum": list(fitness_service.INTENSITIES),
                          "description": "强度，默认中"},
            "calories": {"type": "integer", "description": "消耗千卡，可空"},
            "weight_kg": {"type": "number", "description": "体重 kg，可空"},
            "notes": {"type": "string", "description": "备注，可空"},
        },
        "required": ["duration_min"],
    },
)
def create_fitness_record(duration_min, date=None, workout_type="其他", intensity="中",
                          calories=None, weight_kg=None, notes=""):
    rec = fitness_service.create_record(
        current_user.id,
        workout_date=date or _today(),
        workout_type=workout_type,
        duration_min=duration_min,
        intensity=intensity,
        calories=calories,
        weight_kg=weight_kg,
        notes=notes or "",
    )
    return f"已记录训练 [id={rec.id}]：{rec.date.isoformat()} {rec.workout_type} {rec.duration_min} 分钟"


@register_tool(
    name="update_fitness_record",
    description="更新已有健身记录。record_id 必填；其余字段只改传入的。",
    parameters={
        "type": "object",
        "properties": {
            "record_id": {"type": "integer", "description": "记录 ID，必填"},
            "date": {"type": "string", "description": "新日期 YYYY-MM-DD"},
            "workout_type": {"type": "string", "description": "新类型"},
            "duration_min": {"type": "integer", "description": "新时长（分钟）"},
            "intensity": {"type": "string", "enum": list(fitness_service.INTENSITIES)},
            "calories": {"type": "integer", "description": "新卡路里；传空字符串可清空"},
            "weight_kg": {"type": "number", "description": "新体重；传空字符串可清空"},
            "notes": {"type": "string", "description": "新备注"},
        },
        "required": ["record_id"],
    },
)
def update_fitness_record(record_id, date=None, workout_type=None, duration_min=None,
                          intensity=None, calories=None, weight_kg=None, notes=None):
    rec = _find(record_id)
    fields = {}
    if date is not None:
        fields["date"] = date
    if workout_type is not None:
        fields["workout_type"] = workout_type
    if duration_min is not None:
        fields["duration_min"] = duration_min
    if intensity is not None:
        fields["intensity"] = intensity
    if calories is not None:
        fields["calories"] = calories
    if weight_kg is not None:
        fields["weight_kg"] = weight_kg
    if notes is not None:
        fields["notes"] = notes
    rec = fitness_service.update_record(rec, **fields)
    return _summary(rec)


@register_tool(
    name="delete_fitness_record",
    description="删除一条健身记录（硬删除，不可恢复）。请确认用户明确要求后再调用。",
    parameters={
        "type": "object",
        "properties": {"record_id": {"type": "integer", "description": "记录 ID，必填"}},
        "required": ["record_id"],
    },
    dangerous=True,
)
def delete_fitness_record(record_id):
    rec = _find(record_id)
    label = f"{rec.date.isoformat()} {rec.workout_type}"
    fitness_service.delete_record(rec)
    return f"🗑️ 已删除健身记录 [id={record_id}]：{label}"


@register_tool(
    name="analyze_fitness",
    description=(
        "根据最近 7 天健身记录生成周分析（频率、类型均衡、建议）。"
        "配置了 AI 时用模型写分析，否则返回统计摘要。"
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)
def analyze_fitness():
    tz = user_tz(current_user)
    today = datetime.now(tz).date()
    start = today - timedelta(days=7)
    records = FitnessRecord.query.filter(
        FitnessRecord.user_id == current_user.id,
        FitnessRecord.date >= start,
        FitnessRecord.date <= today,
    ).order_by(FitnessRecord.date.asc()).all()
    if not records:
        return "最近 7 天还没有健身记录。"
    return fitness_service.build_weekly_analysis(records, current_user)
