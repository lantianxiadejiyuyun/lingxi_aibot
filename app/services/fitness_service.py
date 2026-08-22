"""健身服务：记录增删改查 + 统计汇总 + AI 周分析 + 调度动作（每日提醒 / 每周分析）。"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from app.extensions import db
from app.models.fitness import FitnessRecord
from app.scheduler import register_action
from app.utils.timeutil import user_tz

logger = logging.getLogger(__name__)

WORKOUT_TYPES = ["跑步", "力量训练", "游泳", "骑行", "瑜伽", "球类", "HIIT", "其他"]
INTENSITIES = ["低", "中", "高"]


# ---------- 解析 ----------

def parse_date(value) -> date:
    """解析 'YYYY-MM-DD'，非法抛 ValueError。"""
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("日期格式不正确（应为 YYYY-MM-DD）") from None


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------- CRUD ----------

def list_records(user_id: int, limit: int = 200) -> list[FitnessRecord]:
    """某用户的全部记录，按日期/ID 倒序（最近在前）。"""
    return FitnessRecord.query.filter(FitnessRecord.user_id == user_id).order_by(
        FitnessRecord.date.desc(), FitnessRecord.id.desc()
    ).limit(limit).all()


def get_record(record_id, user_id: Optional[int] = None) -> Optional[FitnessRecord]:
    rec = db.session.get(FitnessRecord, record_id)
    if rec is not None and user_id is not None and rec.user_id != user_id:
        return None
    return rec


def create_record(user_id: int, workout_date, workout_type: str, duration_min, intensity: str,
                  calories=None, weight_kg=None, notes: str = "") -> FitnessRecord:
    """新建记录。日期非法 / 时长为负抛 ValueError。"""
    d = parse_date(workout_date)
    wtype = (workout_type or "其他").strip() or "其他"
    mins = _to_int(duration_min)
    if mins < 0:
        raise ValueError("时长不能为负数")
    rec = FitnessRecord(
        user_id=user_id,
        date=d,
        workout_type=wtype,
        duration_min=mins,
        intensity=(intensity or "中").strip() or "中",
        calories=_to_int(calories) if calories not in (None, "") else None,
        weight_kg=_to_float(weight_kg),
        notes=notes or "",
    )
    db.session.add(rec)
    db.session.commit()
    return rec


def update_record(rec: FitnessRecord, **fields) -> FitnessRecord:
    """更新记录；值为 None 的字段不修改。"""
    if "date" in fields and fields["date"] is not None:
        rec.date = parse_date(fields["date"])
    if "workout_type" in fields and fields["workout_type"] is not None:
        rec.workout_type = str(fields["workout_type"]).strip() or "其他"
    if "duration_min" in fields and fields["duration_min"] is not None:
        mins = _to_int(fields["duration_min"])
        if mins < 0:
            raise ValueError("时长不能为负数")
        rec.duration_min = mins
    if "intensity" in fields and fields["intensity"] is not None:
        rec.intensity = str(fields["intensity"]).strip() or "中"
    if "calories" in fields:
        rec.calories = _to_int(fields["calories"]) if fields["calories"] not in (None, "") else None
    if "weight_kg" in fields:
        rec.weight_kg = _to_float(fields["weight_kg"])
    if "notes" in fields and fields["notes"] is not None:
        rec.notes = str(fields["notes"])
    db.session.commit()
    return rec


def delete_record(rec: FitnessRecord) -> None:
    db.session.delete(rec)
    db.session.commit()


# ---------- 统计 ----------

def summary(records: list[FitnessRecord]) -> dict:
    """统计：次数 / 总时长 / 总卡路里 / 类型分布 / 平均体重 / 最近体重。"""
    total = len(records)
    total_min = sum(r.duration_min or 0 for r in records)
    total_cal = sum(r.calories or 0 for r in records)
    by_type: dict[str, int] = {}
    for r in records:
        by_type[r.workout_type] = by_type.get(r.workout_type, 0) + 1
    weights = [r.weight_kg for r in records if r.weight_kg]
    return {
        "count": total,
        "total_min": total_min,
        "total_cal": total_cal,
        "by_type": by_type,
        "avg_weight": round(sum(weights) / len(weights), 1) if weights else None,
        "latest_weight": weights[-1] if weights else None,
    }


def _records_to_text(records: list[FitnessRecord], tz) -> str:
    lines = []
    for r in records:
        extra = []
        if r.calories:
            extra.append(f"{r.calories}千卡")
        if r.weight_kg:
            extra.append(f"体重{r.weight_kg}kg")
        tail = f"（{'，'.join(extra)}）" if extra else ""
        lines.append(f"- {r.date.isoformat()} {r.workout_type} {r.duration_min}分钟 强度{r.intensity}{tail}")
    return "\n".join(lines) or "- 无"


# ---------- AI 周分析 ----------

def build_weekly_analysis(records: list[FitnessRecord], user) -> str:
    """根据最近记录生成周分析文本（LLM 优先，失败降级为统计 Markdown）。"""
    tz = user_tz(user)
    s = summary(records)
    stats_text = (f"本周共 {s['count']} 次训练，总时长 {s['total_min']} 分钟，"
                  f"总消耗 {s['total_cal']} 千卡")
    if s["by_type"]:
        type_text = "、".join(f"{k}×{v}" for k, v in sorted(s["by_type"].items(), key=lambda x: -x[1]))
    else:
        type_text = "无"
    if s["latest_weight"]:
        stats_text += f"；类型分布 {type_text}；最近体重 {s['latest_weight']}kg"
    else:
        stats_text += f"；类型分布 {type_text}"

    detail = _records_to_text(records, tz)

    from app.ai.llm import LLMClient, LLMError

    llm = LLMClient()
    if llm.is_configured:
        prompt = (
            "你是专业健身教练。请根据以下最近 7 天的健身记录做简要分析：\n"
            f"{stats_text}\n\n明细：\n{detail}\n\n"
            "要求：分析训练频率、类型均衡度、时长趋势，并给出 2~3 条具体可执行的建议。"
            "用中文、Markdown 格式，200 字以内，语气友好。"
        )
        try:
            content, _ = llm.chat([
                {"role": "system", "content": "你是专业健身教练，建议具体、可执行、不夸大。"},
                {"role": "user", "content": prompt},
            ])
            if content.strip():
                return content
        except LLMError as e:  # noqa: BLE001
            logger.warning("健身周分析 LLM 失败，降级为统计: %s", e)

    return (
        f"{stats_text}\n\n"
        f"## 本周明细\n{detail}\n\n"
        "## 建议\n- 保持每周 3~5 次训练，注意训练与休息的平衡。\n"
        "- 有氧与力量结合，逐步提升强度或时长。"
    )


# ---------- 调度动作 ----------

@register_action("fitness_reminder", description="每日健身提醒（参数：channel 或 channels）")
def fitness_reminder(user, params: dict | None = None) -> None:
    """每日健身提醒：固定时间推一条提醒。"""
    from app.services.notify_service import notify_for

    params = params or {}
    notify_for(
        "reminder",
        "🏋️ 健身提醒",
        "该去健身啦！今天也别忘了动一动，哪怕只是散步 20 分钟。",
        user_id=user.id,
        explicit=params.get("channels") or params.get("channel"),
    )


@register_action("fitness_weekly_analysis", description="每周健身分析（参数：channel 或 channels）")
def weekly_analysis(user, params: dict | None = None) -> None:
    """每周健身分析：汇总该用户最近 7 天记录，AI 分析后推送。"""
    from app.services.notify_service import notify_for

    params = params or {}
    tz = user_tz(user)
    today = datetime.now(tz).date()
    start = today - timedelta(days=7)
    records = FitnessRecord.query.filter(
        FitnessRecord.user_id == user.id,
        FitnessRecord.date >= start, FitnessRecord.date <= today
    ).order_by(FitnessRecord.date.asc()).all()

    if not records:
        content = "本周还没有健身记录。动起来，从一次快走开始吧！💪"
    else:
        content = build_weekly_analysis(records, user)

    notify_for(
        "report",
        "📊 健身周分析",
        content,
        user_id=user.id,
        explicit=params.get("channels") or params.get("channel"),
    )
