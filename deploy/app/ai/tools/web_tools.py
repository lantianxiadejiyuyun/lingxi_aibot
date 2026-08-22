"""联网 AI 工具：web_search（搜索并返回带 HTML 链接的结果）+ fetch_page（抓取网页正文）。"""
from __future__ import annotations

from app.ai.registry import register_tool
from app.services import web_search_service


@register_tool(
    name="web_search",
    description=(
        "联网搜索。query 为搜索关键词（必填，中文英文皆可）；limit 为返回条数（默认 5，最大 10）。"
        "返回 [{title, url, snippet}] 的搜索结果。"
        "调研/搜索任务的标准流程：搜索完成后先用 create_page 把结果整理成「调研网页」"
        "（结构化 HTML，含结论与可点击链接），再回复网页地址；用户要求收录知识库时，"
        "再用 create_note 提炼核心保存为笔记（tags 加“调研”）。"
        "适合回答“最近/最新/查找/调查/了解一下…”等需要互联网信息的问题；"
        "用户提到要“搜一下/查一下/上网找/调研”时优先调用本工具。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词，必填"},
            "limit": {"type": "integer", "description": "返回条数，默认 5，最大 10"},
        },
        "required": ["query"],
    },
)
def web_search(query: str, limit: int = 5):
    try:
        results = web_search_service.search(query, limit=limit)
    except web_search_service.SearchError as e:
        raise ValueError(str(e)) from e
    if not results:
        return "没有找到相关结果，换个关键词试试。"
    return results


@register_tool(
    name="fetch_page",
    description=(
        "抓取一个网页的正文文本（自动去除脚本/样式/标签），供阅读与调查。"
        "url 为网页地址（必填，http/https）；max_chars 为最多返回字符数（默认 6000，最大 20000）。"
        "适合在 web_search 找到结果后进一步阅读页面内容、提炼信息。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "网页地址，必填"},
            "max_chars": {"type": "integer", "description": "最多返回字符数，默认 6000，最大 20000"},
        },
        "required": ["url"],
    },
)
def fetch_page(url: str, max_chars: int = 6000):
    try:
        text = web_search_service.fetch_page(url, max_chars=max_chars)
    except web_search_service.SearchError as e:
        raise ValueError(str(e)) from e
    if not text:
        return "页面没有可读的文本内容。"
    return text
