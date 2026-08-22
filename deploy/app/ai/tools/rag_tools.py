"""RAG AI 工具：语义检索笔记 / 网页 / 对话内容（未配置嵌入服务时降级关键词搜索）。"""
from __future__ import annotations

from app.ai.registry import register_tool
from app.services import rag_service


@register_tool(
    name="semantic_search",
    description=(
        "语义检索用户的历史内容（笔记/网页/对话）。query 为检索内容（必填）；"
        "sources 为限定范围（可选数组，可含 note/webpage/conversation，省略则全部）；"
        "k 为返回条数（默认 5，最大 10）。"
        "返回命中的 source_type/source_id/score 与文本片段，"
        "适合回答“我什么时候提过…”“我有没有写过关于…的内容”类问题；"
        "未配置语义检索服务时自动降级为关键词搜索并在结果中注明。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索内容，必填"},
            "sources": {
                "type": "array", "items": {"type": "string", "enum": ["note", "webpage", "conversation"]},
                "description": "限定来源：note/webpage/conversation，可选",
            },
            "k": {"type": "integer", "description": "返回条数，默认 5，最大 10"},
        },
        "required": ["query"],
    },
)
def semantic_search(query: str, sources: list | None = None, k: int = 5):
    results = rag_service.semantic_search(query, sources=sources, k=k)
    if results:
        return results
    # 降级：关键词搜索笔记
    hint = ""
    if not rag_service.is_configured():
        hint = "（未配置语义检索服务，已降级为关键词搜索；可在「设置 → AI 设置」配置嵌入接口）"
    from app.services import note_service

    notes = note_service.search_notes(query, limit=5)
    if notes:
        return {"降级关键词结果": notes, "note": hint} if hint else {"降级关键词结果": notes}
    return hint or "没有找到相关内容。"
