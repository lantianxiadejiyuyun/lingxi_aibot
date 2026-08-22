"""技能 AI 工具：AI 自写 skill 的创建 / 查询 / 更新 / 启停 / 删除（自进化核心）。

- create_skill：AI 编写 Python 函数体，存为技能（默认禁用，需用户在「技能」页人工启用）
- code 是函数体（不含 def 行），参数名与 parameters.properties 的键一致
- 受限沙箱：无 os/subprocess/网络/文件/反射，只能调 db 与注入的服务
"""
from __future__ import annotations

from app.ai.registry import register_tool
from app.services import skill_service
from app.utils.timeutil import fmt_dt, user_tz


def _find(skill_id):
    try:
        skill_id = int(skill_id)
    except (TypeError, ValueError):
        raise ValueError("skill_id 必须是数字") from None
    skill = skill_service.get_skill(skill_id)
    if skill is None:
        raise ValueError(f"技能 {skill_id} 不存在或已删除")
    return skill


def _json(skill, include_code: bool = False) -> dict:
    from flask_login import current_user

    data = {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "enabled": skill.enabled,
        "updated_at": fmt_dt(skill.updated_at, user_tz(current_user)),
    }
    if include_code:
        data["parameters"] = skill.parameters
        data["code"] = skill.code
    return data


@register_tool(
    name="get_skill_template",
    description=(
        "获取「技能编写规范」：参数签名推导规则、返回值约定、受限环境可用模块、"
        "禁止项与一个完整示例。编写新技能（create_skill）前应先调用它，确保代码符合规范。"
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)
def get_skill_template():
    return skill_service.skill_template()


@register_tool(
    name="create_skill",
    description=(
        "创建一个新的技能（AI 自写工具，自进化能力）。请先调用 get_skill_template 获取编写规范。"
        "name 为英文技能名（snake_case，必填）；"
        "description 为用途描述（模型据此决定何时调用，必填）；"
        "parameters 为 JSON Schema（需含 type=object 与 properties，properties 的键即函数参数名，必填）；"
        "code 为 Python 函数体（不含 def 行，直接用参数名，返回值需为 str 或可 JSON 序列化对象，必填）。"
        "技能在受限沙箱的子进程中执行（超时 10 秒强杀）：不能 import/os/subprocess/网络/文件操作，"
        "可用 db、json、datetime、re 及注入的服务"
        "（calendar_service/task_service/note_service/job_service/notify_service/image_service/page_service）。"
        "技能创建后默认禁用，需提醒用户到「技能」页人工启用后才会生效。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名，snake_case，如 summarize_text"},
            "description": {"type": "string", "description": "用途描述，模型据此决定何时调用"},
            "parameters": {
                "type": "object",
                "description": "JSON Schema：含 type=object 与 properties（键=参数名）",
            },
            "code": {"type": "string", "description": "Python 函数体（不含 def 行）"},
        },
        "required": ["name", "description", "parameters", "code"],
    },
    dangerous=True,
)
def create_skill(name: str, description: str, parameters: dict, code: str):
    try:
        skill = skill_service.create_skill(name, description, parameters, code)
    except skill_service.SkillError as e:
        raise ValueError(str(e)) from e
    return (f"已创建技能 [id={skill.id}]「{skill.name}」（默认禁用）。"
            f"请提醒用户到「🧩 技能」页人工启用后，该技能才会在对话中可用。")


@register_tool(
    name="list_skills",
    description="列出全部技能（含禁用与启用），返回 id/name/description/enabled/更新时间。",
    parameters={"type": "object", "properties": {}, "required": []},
)
def list_skills():
    skills = skill_service.list_skills()
    if not skills:
        return "还没有创建过技能。"
    return [_json(s) for s in skills[:50]]


@register_tool(
    name="get_skill",
    description="获取某技能的完整信息（含 JSON Schema 与代码），修改技能前先读取。skill_id 必填。",
    parameters={
        "type": "object",
        "properties": {"skill_id": {"type": "integer", "description": "技能 ID，必填"}},
        "required": ["skill_id"],
    },
)
def get_skill(skill_id: int):
    return _json(_find(skill_id), include_code=True)


@register_tool(
    name="update_skill",
    description=(
        "修改已有技能。skill_id 必填；其余参数只改传入的字段。"
        "code 为函数体、parameters 为 JSON Schema、description 为用途描述。"
        "若技能已启用，更新后立即重新加载生效。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {"type": "integer", "description": "技能 ID，必填"},
            "description": {"type": "string", "description": "新用途描述，可选"},
            "parameters": {"type": "object", "description": "新 JSON Schema，可选"},
            "code": {"type": "string", "description": "新函数体，可选"},
        },
        "required": ["skill_id"],
    },
    dangerous=True,
)
def update_skill(skill_id: int, description: str | None = None,
                 parameters: dict | None = None, code: str | None = None):
    skill = _find(skill_id)
    try:
        skill_service.update_skill(skill, description=description,
                                   parameters=parameters, code=code)
    except skill_service.SkillError as e:
        raise ValueError(str(e)) from e
    return f"已更新技能 [id={skill.id}]「{skill.name}」（{'已启用' if skill.enabled else '已禁用'}）"


@register_tool(
    name="set_skill_enabled",
    description=(
        "启用或停用技能。skill_id 必填；enabled=true 启用（会立即编译并注册执行），false 停用。"
        "启用会执行用户编写的代码，请确认用户明确要求后再调用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill_id": {"type": "integer", "description": "技能 ID，必填"},
            "enabled": {"type": "boolean", "description": "true 启用 / false 停用，必填"},
        },
        "required": ["skill_id", "enabled"],
    },
    dangerous=True,
)
def set_skill_enabled(skill_id: int, enabled: bool):
    skill = _find(skill_id)
    try:
        skill_service.set_enabled(skill, enabled)
    except skill_service.SkillError as e:
        raise ValueError(str(e)) from e
    return f"技能「{skill.name}」已{'启用' if skill.enabled else '停用'}"


@register_tool(
    name="delete_skill",
    description="删除技能（软删除，不可恢复，确认用户明确要求后再调用）。skill_id 必填。",
    parameters={
        "type": "object",
        "properties": {"skill_id": {"type": "integer", "description": "技能 ID，必填"}},
        "required": ["skill_id"],
    },
    dangerous=True,
)
def delete_skill(skill_id: int):
    skill = _find(skill_id)
    name = skill.name
    skill_service.soft_delete(skill)
    return f"🗑️ 已删除技能「{name}」"
