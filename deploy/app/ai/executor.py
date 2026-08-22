"""对话执行器：把一次用户发言跑完整个「多轮工具调用」循环。

run_chat 是生成器，事件元组：
  ("delta", str)      流式文本增量
  ("tool", dict)      一次工具调用结果 {name, arguments, result, ok}
  ("title", str)      新会话标题
  ("done", str)       最终助手回答
  ("error", str)      出错信息
"""
from __future__ import annotations

import json
import logging

from flask import current_app

from app.ai import registry
from app.ai.llm import LLMClient
from app.ai.memory import build_messages
from app.extensions import db
from app.models.conversation import Message
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)


def _openai_tool_calls(calls: list[dict]) -> list[dict]:
    """扁平调用列表 [{id,name,arguments}] → OpenAI messages 格式。"""
    return [
        {
            "id": c.get("id", ""),
            "type": "function",
            "function": {"name": c.get("name", ""), "arguments": c.get("arguments", "")},
        }
        for c in calls
    ]


def run_chat(conversation, user_text, user):
    """执行一轮对话（含工具调用），yield 事件供前端流式展示。"""
    try:
        # 1) 持久化用户消息（显式 conversation_id，避免关系集合懒加载触发
        #    提前 autoflush 导致 conversation_id 为 NULL）
        user_msg = Message(role="user", content=user_text, conversation_id=conversation.id)
        db.session.add(user_msg)
        db.session.commit()

        # 2) 新会话：生成标题（除刚存这条外没有其他消息）
        others = [m for m in conversation.messages if m is not user_msg and m.role in ("user", "assistant")]
        if not others:
            title = user_text[:20] or "新对话"
            conversation.title = title
            conversation.updated_at = utcnow()
            db.session.commit()
            yield ("title", title)

        # 3) 组装 messages（含刚存的用户消息，tool 消息不回灌）
        messages = build_messages(conversation, user)

        # 4) 多轮工具调用循环
        max_rounds = current_app.config.get("LLM_MAX_TOOL_ROUNDS", 8)
        llm = LLMClient()
        final_content = ""
        tool_calls_json: list[dict] = []

        for _round in range(max_rounds):
            content_parts: list[str] = []
            tool_calls: list[dict] = []
            for ev in llm.chat_stream(messages, tools=registry.openai_tools()):
                if ev["type"] == "delta":
                    content_parts.append(ev["text"])
                    yield ("delta", ev["text"])
                elif ev["type"] == "tool_calls":
                    tool_calls = ev["calls"]

            content = "".join(content_parts)
            final_content = content

            # 追加本轮 assistant 消息（含工具调用声明）
            assistant_msg: dict = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = _openai_tool_calls(tool_calls)
            messages.append(assistant_msg)

            if not tool_calls:
                break

            # 执行工具，结果回灌 messages 继续下一轮
            for call in tool_calls:
                try:
                    arguments = json.loads(call.get("arguments") or "{}")
                    if not isinstance(arguments, dict):
                        arguments = {"value": arguments}
                except (ValueError, TypeError):
                    result_str = json.dumps({"error": "参数解析失败"}, ensure_ascii=False)
                else:
                    result_str = registry.execute_tool(call.get("name", ""), arguments)

                ok = "error" not in result_str
                tool_calls_json.append(call)
                yield ("tool", {
                    "name": call.get("name", ""),
                    "arguments": call.get("arguments", ""),
                    "result": result_str[:200],
                    "ok": ok,
                })
                # 工具结果落库（历史记录展示用；不回灌 LLM 上下文）
                db.session.add(Message(
                    role="tool",
                    content=result_str,
                    conversation=conversation,
                ))
                messages.append({
                    "role": "tool",
                    "content": result_str,
                    "tool_call_id": call.get("id", ""),
                })

        # 5) 存储最终助手回答
        conversation.updated_at = utcnow()
        db.session.add(Message(
            role="assistant",
            content=final_content,
            tool_calls=tool_calls_json or None,
            conversation=conversation,
        ))
        db.session.commit()
        yield ("done", final_content)

    except Exception as e:  # noqa: BLE001 —— 对话异常不中断请求，回传错误
        logger.exception("对话执行失败")
        db.session.rollback()  # 回滚未提交部分，已提交的用户消息保留
        yield ("error", str(e)[:500])
