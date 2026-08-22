"""出行 AI 工具：查询 / 新建 / 更新 / 生成行程 / 删除。"""
from __future__ import annotations

from flask_login import current_user

from app.ai.registry import register_tool
from app.models.travel import TripPlan
from app.services import travel_service

_TRANSPORTS = "、".join(travel_service.TRANSPORT_OPTIONS)


def _find(plan_id) -> TripPlan:
    try:
        pid = int(plan_id)
    except (TypeError, ValueError):
        raise ValueError("plan_id 必须是数字") from None
    plan = travel_service.get_plan(pid, current_user.id)
    if plan is None:
        raise ValueError(f"出行计划 {pid} 不存在")
    return plan


def _brief(plan: TripPlan) -> dict:
    return {
        "id": plan.id,
        "destination": plan.destination,
        "start_date": plan.start_date.isoformat(),
        "end_date": plan.end_date.isoformat(),
        "transports": list(plan.transports or []),
        "segments_count": len(plan.segments or []),
        "alternatives_count": len(plan.alternatives or []),
        "budget": plan.budget,
        "companions": plan.companions or "",
        "has_itinerary": bool((plan.itinerary or "").strip()),
    }


def _detail(plan: TripPlan) -> dict:
    data = _brief(plan)
    data.update({
        "segments": list(plan.segments or []),
        "alternatives": list(plan.alternatives or []),
        "notes": plan.notes or "",
        "itinerary": plan.itinerary or "",
    })
    return data


@register_tool(
    name="list_trip_plans",
    description=(
        "列出出行计划（摘要，不含完整行程正文）。按出发日期倒序。"
        "需要看多段交通或 AI 行程时再用 get_trip_plan。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "最多返回条数，默认 20"},
        },
        "required": [],
    },
)
def list_trip_plans(limit=20):
    try:
        limit = max(1, min(100, int(limit or 20)))
    except (TypeError, ValueError):
        limit = 20
    plans = travel_service.list_plans(current_user.id, limit=limit)
    if not plans:
        return "还没有出行计划。"
    return [_brief(p) for p in plans]


@register_tool(
    name="get_trip_plan",
    description="获取一条出行计划的完整详情（含多段交通、备选方案、AI 行程正文）。plan_id 必填。",
    parameters={
        "type": "object",
        "properties": {"plan_id": {"type": "integer", "description": "计划 ID，必填"}},
        "required": ["plan_id"],
    },
)
def get_trip_plan(plan_id):
    return _detail(_find(plan_id))


@register_tool(
    name="create_trip_plan",
    description=(
        f"新建出行计划。destination / start_date / end_date 必填（日期 YYYY-MM-DD）。"
        f"transports 交通方式多选：{_TRANSPORTS}；"
        "segments 为多段交通列表，每段 type/from/to/depart_time/arrive_time/duration/platform/note；"
        "alternatives 为备选方案列表，每项 title/desc；budget 预算（元）；companions 同行人；notes 备注。"
        "创建后可用 generate_trip_itinerary 生成逐日行程。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "destination": {"type": "string", "description": "目的地，必填"},
            "start_date": {"type": "string", "description": "出发日期 YYYY-MM-DD，必填"},
            "end_date": {"type": "string", "description": "返回日期 YYYY-MM-DD，必填"},
            "transports": {
                "type": "array", "items": {"type": "string"},
                "description": f"交通方式，可选：{_TRANSPORTS}",
            },
            "segments": {
                "type": "array",
                "items": {"type": "object"},
                "description": "多段交通，每段含 type/from/to/depart_time/arrive_time/duration/platform/note",
            },
            "alternatives": {
                "type": "array",
                "items": {"type": "object"},
                "description": "备选方案，每项 title/desc",
            },
            "budget": {"type": "integer", "description": "预算（元），可空"},
            "companions": {"type": "string", "description": "同行人，可空"},
            "notes": {"type": "string", "description": "偏好/备注，可空"},
        },
        "required": ["destination", "start_date", "end_date"],
    },
)
def create_trip_plan(destination, start_date, end_date, transports=None, segments=None,
                     alternatives=None, budget=None, companions="", notes=""):
    plan = travel_service.create_plan(
        current_user.id,
        destination=destination,
        start_date=start_date,
        end_date=end_date,
        transports=transports,
        segments=segments,
        alternatives=alternatives,
        budget=budget,
        companions=companions or "",
        notes=notes or "",
    )
    return f"已创建出行计划 [id={plan.id}]：{plan.destination} {plan.start_date.isoformat()} → {plan.end_date.isoformat()}"


@register_tool(
    name="update_trip_plan",
    description="更新已有出行计划。plan_id 必填；其余字段只改传入的。",
    parameters={
        "type": "object",
        "properties": {
            "plan_id": {"type": "integer", "description": "计划 ID，必填"},
            "destination": {"type": "string"},
            "start_date": {"type": "string", "description": "YYYY-MM-DD"},
            "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            "transports": {"type": "array", "items": {"type": "string"}},
            "segments": {"type": "array", "items": {"type": "object"}},
            "alternatives": {"type": "array", "items": {"type": "object"}},
            "budget": {"type": "integer"},
            "companions": {"type": "string"},
            "notes": {"type": "string"},
        },
        "required": ["plan_id"],
    },
)
def update_trip_plan(plan_id, destination=None, start_date=None, end_date=None,
                     transports=None, segments=None, alternatives=None,
                     budget=None, companions=None, notes=None):
    plan = _find(plan_id)
    fields = {}
    for key, val in (
        ("destination", destination), ("start_date", start_date), ("end_date", end_date),
        ("transports", transports), ("segments", segments), ("alternatives", alternatives),
        ("budget", budget), ("companions", companions), ("notes", notes),
    ):
        if val is not None:
            fields[key] = val
    plan = travel_service.update_plan(plan, **fields)
    return _brief(plan)


@register_tool(
    name="generate_trip_itinerary",
    description=(
        "根据出行计划（目的地、日期、多段交通、备选、预算、备注）用 AI 生成逐日行程，"
        "并保存到该计划。plan_id 必填。未配置 AI 时返回提示文案。"
    ),
    parameters={
        "type": "object",
        "properties": {"plan_id": {"type": "integer", "description": "计划 ID，必填"}},
        "required": ["plan_id"],
    },
)
def generate_trip_itinerary(plan_id):
    plan = _find(plan_id)
    content = travel_service.generate_itinerary(plan, current_user)
    travel_service.update_plan(plan, itinerary=content)
    preview = (content or "").strip().replace("\n", " ")[:120]
    return f"已为「{plan.destination}」生成行程 [id={plan.id}]：{preview}…"


@register_tool(
    name="delete_trip_plan",
    description="删除一条出行计划（硬删除，不可恢复）。请确认用户明确要求后再调用。",
    parameters={
        "type": "object",
        "properties": {"plan_id": {"type": "integer", "description": "计划 ID，必填"}},
        "required": ["plan_id"],
    },
    dangerous=True,
)
def delete_trip_plan(plan_id):
    plan = _find(plan_id)
    dest = plan.destination
    travel_service.delete_plan(plan)
    return f"🗑️ 已删除出行计划 [id={plan_id}]：{dest}"
