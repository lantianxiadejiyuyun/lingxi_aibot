"""对话记忆组装：系统提示词 + 全局长期记忆 + 会话摘要 + 最近消息。"""
from __future__ import annotations

from app.ai.prompts import build_system_prompt
from app.services import context_service, memory_service


def build_messages(conversation, user) -> list[dict]:
    """组装发给 LLM 的 messages。

    - 首条为 system 提示词（含当前时间与用户时区）
    - 第二条为全局长期记忆（memories 表，有则注入）
    - 第三条为会话摘要（conversation.summary，有则注入）
    - 最后取尚未被摘要覆盖的 user / assistant 原文（tool 消息不回灌）
      无边界的旧会话保留全部现存消息，避免静默丢弃未总结的历史
    """
    messages: list[dict] = [{"role": "system", "content": build_system_prompt(user, conversation=conversation)}]
    mem = memory_service.build_memory_context(user.id)
    if mem:
        messages.append({"role": "system", "content": mem})
    with context_service.conversation_lock(conversation.id):
        # active_messages 刷新摘要与边界；随后在同一把锁下组装，避免混用旧摘要。
        recent = context_service.active_messages(conversation, user)
        if conversation.summary:
            messages.append({"role": "system", "content": f"本会话历史摘要：\n{conversation.summary}"})
        for m in recent:
            if m.role in ("user", "assistant") and m.content:
                messages.append({"role": m.role, "content": m.content})
    return messages
