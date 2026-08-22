"""对话记忆组装：系统提示词 + 全局长期记忆 + 会话摘要 + 最近消息。"""
from __future__ import annotations

from app.ai.prompts import build_system_prompt
from app.services import memory_service


def build_messages(conversation, user) -> list[dict]:
    """组装发给 LLM 的 messages。

    - 首条为 system 提示词（含当前时间与用户时区）
    - 第二条为全局长期记忆（memories 表，有则注入）
    - 第三条为会话摘要（conversation.summary，有则注入）
    - 最后取最近消息中 role ∈ {user, assistant} 且 content 非空的
      （有摘要时保留最近 8 条，否则最近 20 条；tool 消息不回灌）
    """
    messages: list[dict] = [{"role": "system", "content": build_system_prompt(user)}]
    mem = memory_service.build_memory_context()
    if mem:
        messages.append({"role": "system", "content": mem})
    if conversation.summary:
        messages.append({"role": "system", "content": f"本会话历史摘要：\n{conversation.summary}"})

    window = memory_service.KEEP_RECENT if conversation.summary else 20
    recent = conversation.messages[-window:] or []
    for m in recent:
        if m.role in ("user", "assistant") and m.content:
            messages.append({"role": m.role, "content": m.content})
    return messages
