"""The model can discover authorized metadata; credentials stay in the protected UI."""
from flask import current_app, has_request_context, url_for

from app.ai.registry import register_tool
from app.services import navigation_vault_reader
from app.utils.integration_api import ApiError
from app.utils.scoping import current_user_id


def _viewer_path():
    if has_request_context():
        return url_for("navigation_vault.index")
    entry = str(current_app.config.get("ADMIN_ENTRY") or "").strip("/")
    with current_app.test_request_context("/", environ_overrides={"SCRIPT_NAME": "/" + entry if entry else ""}):
        return url_for("navigation_vault.index")


@register_tool(
    name="search_navigation_vault",
    description=("用户需要查找已授权导航站保险库条目时，搜索条目名称、站点和账号掩码。"
                 "密码只在受保护页面点击查看，不返回模型。没有授权时不能读取。"),
    parameters={"type": "object", "properties": {"query": {
        "type": "string", "description": "名称或站点关键词，最多 200 个字符"}},
        "required": ["query"], "additionalProperties": False},
)
def search_navigation_vault(query: str):
    user_id = current_user_id()
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("请先登录后查找已授权保险库")
    if not isinstance(query, str) or len(query) > 200:
        raise ValueError("query 必须是最多 200 个字符的文本")
    try:
        query.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("query 包含无效字符") from None
    try:
        result = navigation_vault_reader.list_vault_items(user_id, q=query.strip(), limit=20, offset=0)
    except ApiError as exc:
        raise ValueError(str(exc)) from None
    except Exception:
        raise ValueError("保险库暂时无法读取，请稍后重试") from None
    items = []
    for item in result["items"][:20]:
        identifier = item.get("id")
        if (not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 200
                or any(ord(character) < 32 for character in identifier)):
            continue
        items.append({key: item.get(key) for key in ("id", "title", "site", "username_masked", "password_set")})
    path = _viewer_path()
    return {"action": {"type": "open_navigation_vault",
                       "list_path": "/integrations/navigation-vault/items",
                       "reveal_path": "/integrations/navigation-vault/reveal"},
            "items": items, "url": path, "url_scope": "lingxi_web",
            "markdown": f"[在灵犀网页查看账号和密码]({path})",
            "message": "仅返回名称、站点与掩码；请在受保护页面点击条目查看完整账号和密码。"}
