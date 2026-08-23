"""AI 工具注册表（基座扩展机制）。

任何模块用 @register_tool 注册工具后，AI 对话即可调用。
app/ai/tools/ 下的所有模块会被自动发现并导入（load_tools）。

工具函数规范：
  - 参数使用 Python 类型注解（用于 schema 的辅助校验）
  - 返回 str 或可 JSON 序列化的对象（execute_tool 统一转为 str）
  - 出错时抛出 ValueError，错误信息会反馈给模型让其自我修正
"""
from __future__ import annotations

import importlib
import json
import logging
import pkgutil
from typing import Any, Callable

logger = logging.getLogger(__name__)

_registry: dict[str, dict] = {}


def register_tool(name: str, description: str, parameters: dict, dangerous: bool = False) -> Callable:
    """注册 AI 工具。

    :param name: 工具名（英文 snake_case）
    :param description: 用途描述（模型依赖它决定何时调用，要写清参数含义）
    :param parameters: JSON Schema（OpenAI function calling 格式，需含 type/properties/required）
    :param dangerous: 是否危险操作（技能变更类工具会在 execute_tool 强制管理员门槛）
    """

    def decorator(fn: Callable) -> Callable:
        if name in _registry:
            logger.warning("工具重复注册，覆盖：%s", name)
        _registry[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "fn": fn,
            "dangerous": dangerous,
        }
        return fn

    return decorator


def get_tool(name: str) -> dict | None:
    return _registry.get(name)


def remove_tool(name: str) -> bool:
    """从注册表移除工具（自写技能注销时用），返回是否成功移除。"""
    return _registry.pop(name, None) is not None


def all_tools() -> list[dict]:
    return list(_registry.values())


def tool_names() -> list[str]:
    return sorted(_registry.keys())


def openai_tools() -> list[dict]:
    """转为 OpenAI function calling 的 tools 参数格式。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in _registry.values()
    ]


# 技能变更类危险工具：任何登录用户都可能诱导模型调用，必须强制管理员门槛。
_SKILL_MUTATION_TOOLS = frozenset({
    "create_skill", "update_skill", "delete_skill", "set_skill_enabled",
})


def _caller_is_admin() -> bool:
    """当前请求调用者是否为管理员（无登录上下文视为否）。"""
    try:
        from flask_login import current_user

        return bool(
            getattr(current_user, "is_authenticated", False)
            and getattr(current_user, "is_admin", False)
        )
    except Exception:  # noqa: BLE001 —— 无请求上下文
        return False


def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """执行工具并返回字符串结果（内容或 JSON 错误）。"""
    tool = _registry.get(name)
    if tool is None:
        return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
    if tool.get("dangerous") and name in _SKILL_MUTATION_TOOLS and not _caller_is_admin():
        return json.dumps(
            {"error": "安全限制：创建/修改/删除技能仅管理员可执行"},
            ensure_ascii=False,
        )
    try:
        args = arguments or {}
        if not isinstance(args, dict):
            args = {"value": args}
        result = tool["fn"](**args)
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)
    except TypeError as e:
        return json.dumps({"error": f"参数错误: {e}"}, ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 —— 工具异常必须回传模型而不是炸掉对话
        logger.exception("工具 %s 执行异常", name)
        return json.dumps({"error": f"执行失败: {e}"}, ensure_ascii=False)


def load_tools() -> list[str]:
    """自动导入 app.ai.tools 包下所有模块，触发其中的 register_tool。"""
    try:
        import app.ai.tools as tools_pkg
    except ImportError:
        logger.warning("app.ai.tools 包不存在，无 AI 工具")
        return []
    imported = []
    for mod in pkgutil.iter_modules(tools_pkg.__path__):
        try:
            importlib.import_module(f"{tools_pkg.__name__}.{mod.name}")
            imported.append(mod.name)
        except Exception:  # noqa: BLE001
            logger.exception("工具模块导入失败: %s", mod.name)
    return imported
