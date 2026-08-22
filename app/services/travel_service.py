"""出行服务：计划增删改查 + 多段交通/备选方案 + AI 生成行程 + 出发提醒调度动作。"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from app.extensions import db
from app.models.travel import TripPlan
from app.scheduler import register_action
from app.utils.timeutil import user_tz

logger = logging.getLogger(__name__)

TRANSPORT_OPTIONS = ["飞机", "高铁", "动车", "火车", "列车", "打车", "自驾", "大巴", "其他"]


def parse_date(value) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("日期格式不正确（应为 YYYY-MM-DD）") from None


def _to_int(value) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_list(value) -> list[str]:
    """多选交通方式归一化：list 或逗号分隔字符串 → 去空去重保序。"""
    if value is None:
        return []
    if isinstance(value, str):
        raw = [t.strip() for t in value.split(",")]
    else:
        raw = [str(t).strip() for t in value]
    seen: set[str] = set()
    out: list[str] = []
    for t in raw:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _segments(value) -> list[dict]:
    """多段交通归一化：只保留含 type 的段，字段补默认值；兼容旧版 time 字段。"""
    if not isinstance(value, list):
        return []
    out = []
    for s in value:
        if not isinstance(s, dict):
            continue
        seg = {
            "type": str(s.get("type") or "").strip() or "其他",
            "from": str(s.get("from") or "").strip(),
            "to": str(s.get("to") or "").strip(),
            "depart_time": str(s.get("depart_time") or s.get("time") or "").strip(),
            "arrive_time": str(s.get("arrive_time") or "").strip(),
            "duration": str(s.get("duration") or "").strip(),
            "platform": str(s.get("platform") or "").strip(),
            "note": str(s.get("note") or "").strip(),
        }
        if seg["type"] or seg["from"] or seg["to"]:
            out.append(seg)
    return out


def _alternatives(value) -> list[dict]:
    """备选方案归一化：保留含 title 或 desc 的项。"""
    if not isinstance(value, list):
        return []
    out = []
    for a in value:
        if not isinstance(a, dict):
            continue
        title = str(a.get("title") or "").strip()
        desc = str(a.get("desc") or "").strip()
        if title or desc:
            out.append({"title": title or "备选方案", "desc": desc})
    return out


# ---------- CRUD ----------

def list_plans(user_id: int, limit: int = 200) -> list[TripPlan]:
    """某用户的全部计划，按出发日期倒序（最近的在前）。"""
    return TripPlan.query.filter(TripPlan.user_id == user_id).order_by(
        TripPlan.start_date.desc(), TripPlan.id.desc()
    ).limit(limit).all()


def get_plan(plan_id, user_id: Optional[int] = None) -> Optional[TripPlan]:
    plan = db.session.get(TripPlan, plan_id)
    if plan is not None and user_id is not None and plan.user_id != user_id:
        return None
    return plan


def create_plan(user_id: int, destination, start_date, end_date, transports=None,
                segments=None, alternatives=None, budget=None, companions="", notes="") -> TripPlan:
    dest = (destination or "").strip()
    if not dest:
        raise ValueError("目的地不能为空")
    start = parse_date(start_date)
    end = parse_date(end_date)
    if end < start:
        raise ValueError("返回日期不能早于出发日期")
    plan = TripPlan(
        user_id=user_id,
        destination=dest,
        start_date=start,
        end_date=end,
        transports=_str_list(transports),
        segments=_segments(segments),
        alternatives=_alternatives(alternatives),
        budget=_to_int(budget),
        companions=(companions or "").strip(),
        notes=notes or "",
        itinerary="",
    )
    db.session.add(plan)
    db.session.commit()
    return plan


def update_plan(plan: TripPlan, **fields) -> TripPlan:
    """更新计划；值为 None 的字段不修改（预算/多选/段/备选允许清空）。"""
    if "destination" in fields and fields["destination"] is not None:
        dest = str(fields["destination"]).strip()
        if not dest:
            raise ValueError("目的地不能为空")
        plan.destination = dest
    if "start_date" in fields and fields["start_date"] is not None:
        plan.start_date = parse_date(fields["start_date"])
    if "end_date" in fields and fields["end_date"] is not None:
        plan.end_date = parse_date(fields["end_date"])
    if plan.end_date < plan.start_date:
        raise ValueError("返回日期不能早于出发日期")
    if "transports" in fields:
        plan.transports = _str_list(fields["transports"])
    if "segments" in fields:
        plan.segments = _segments(fields["segments"])
    if "alternatives" in fields:
        plan.alternatives = _alternatives(fields["alternatives"])
    if "budget" in fields:
        plan.budget = _to_int(fields["budget"])
    if "companions" in fields and fields["companions"] is not None:
        plan.companions = str(fields["companions"]).strip()
    if "notes" in fields and fields["notes"] is not None:
        plan.notes = str(fields["notes"])
    if "itinerary" in fields and fields["itinerary"] is not None:
        plan.itinerary = str(fields["itinerary"])
    db.session.commit()
    return plan


def delete_plan(plan: TripPlan) -> None:
    db.session.delete(plan)
    db.session.commit()


# ---------- AI 生成行程 ----------

def _segments_text(plan: TripPlan) -> str:
    if not plan.segments:
        return "（未填写多段交通，请按目的地合理规划）"
    lines = []
    for i, s in enumerate(plan.segments, 1):
        parts = [f"{i}. {s['type']}"]
        if s["from"] or s["to"]:
            parts.append(f"{s['from'] or '?'} → {s['to'] or '?'}")
        if s["depart_time"]:
            parts.append(f"搭乘 {s['depart_time']}")
        if s["arrive_time"]:
            parts.append(f"到达 {s['arrive_time']}")
        if s["duration"]:
            parts.append(f"历时 {s['duration']}")
        if s["platform"]:
            parts.append(f"购票 {s['platform']}")
        if s["note"]:
            parts.append(f"（{s['note']}）")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _alternatives_text(plan: TripPlan) -> str:
    if not plan.alternatives:
        return "（无备选方案）"
    lines = []
    for a in plan.alternatives:
        lines.append(f"- {a['title']}：{a['desc']}")
    return "\n".join(lines)


def generate_itinerary(plan: TripPlan, user) -> str:
    """根据计划信息（含多段交通/备选方案）用 LLM 生成逐日行程（Markdown）。"""
    from app.ai.llm import LLMClient, LLMError

    days = (plan.end_date - plan.start_date).days + 1
    transports = "、".join(plan.transports) if plan.transports else "未定"
    info = (
        f"目的地：{plan.destination}\n"
        f"出发：{plan.start_date.isoformat()}，返回：{plan.end_date.isoformat()}（共 {days} 天）\n"
        f"涉及交通：{transports}\n"
        f"多段交通安排：\n{_segments_text(plan)}\n"
        f"备选方案：\n{_alternatives_text(plan)}\n"
        f"预算：{plan.budget if plan.budget is not None else '未定'}元\n"
        f"同行人：{plan.companions or '无'}\n"
        f"偏好/备注：{plan.notes or '无'}"
    )
    prompt = (
        "你是资深旅行规划师。请根据以下出行信息，生成一份逐日行程规划：\n\n"
        f"{info}\n\n"
        f"要求：按 {days} 天逐日列出（Day 1 … Day {days}），每天包含上午/下午/晚上安排与餐饮/住宿建议；"
        "优先使用给出的多段交通与备选方案，兼顾换乘衔接、当地特色，并给出 3 条出行小贴士。"
        "用中文 Markdown 格式，简洁可执行。"
    )

    llm = LLMClient()
    if not llm.is_configured:
        return "未配置 AI：请在「设置 → 模型与人设」填写你自己的 API Key 后再生成行程。"
    try:
        content, _ = llm.chat([
            {"role": "system", "content": "你是资深旅行规划师，行程具体、可执行、贴合偏好。"},
            {"role": "user", "content": prompt},
        ])
        return content.strip() or "（生成结果为空，请重试）"
    except LLMError as e:  # noqa: BLE001
        logger.warning("生成行程失败: %s", e)
        return f"生成行程失败：{e}"


# ---------- 调度动作 ----------

@register_action("trip_reminder", description="出发前一天提醒（参数：channel 或 channels）")
def trip_reminder(user, params: dict | None = None) -> None:
    """出发前一天提醒：扫描该用户明天出发的计划，推一条汇总提醒。"""
    from app.services.notify_service import notify_for

    params = params or {}
    tz = user_tz(user)
    tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
    plans = TripPlan.query.filter(
        TripPlan.user_id == user.id, TripPlan.start_date == tomorrow
    ).all()
    if not plans:
        return
    lines = "\n".join(
        f"- {p.destination}（{p.start_date.isoformat()} 出发"
        f"{'，' + '、'.join(p.transports) if p.transports else ''}）"
        for p in plans
    )
    notify_for(
        "reminder",
        "🧳 出行提醒",
        f"明天有出行安排：\n{lines}\n\n记得提前收拾行李、确认车票/机票和行程哦。",
        user_id=user.id,
        explicit=params.get("channels") or params.get("channel"),
    )
