"""记忆服务：会话摘要 + 全局长期记忆 + 上下文注入 + 定时梳理动作。

- 会话摘要：长对话压缩为 conv.summary，按摘要边界选上下文，完整聊天记录始终保留
- 长期记忆：跨对话抽取事实/偏好存 memories 表（source=auto 每次梳理整体替换，
  source=manual 由用户/AI 手动记录、不被自动覆盖）；每条带重要度 1-5 与过期时间
- 上下文注入：build_messages 注入 全局记忆（过滤过期、按重要度排序）+ 会话摘要 + 最近消息
- 定时动作：context_consolidation（内置任务，默认每日 04:00）
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from app.extensions import db
from app.models.conversation import Conversation, Message
from app.models.memory import SOURCE_AUTO, SOURCE_MANUAL, Memory
from app.models.user import User
from app.scheduler import register_action
from app.utils.timeutil import get_tz, parse_local, utcnow

logger = logging.getLogger(__name__)

KEEP_RECENT = 8          # 有摘要时保留的最近消息条数
CONSOLIDATE_MIN = 12     # 未声明模型容量的旧配置保留原定时梳理门槛
MEMORY_LIMIT = 50        # 注入上下文的最大记忆条数
MEMORY_TRUNCATE = 300    # 单条记忆注入时截断长度

# 抽取输出格式：内容 | 重要度1-5 | 过期时间（YYYY-MM-DD，无则 无）
_MEM_LINE_RE = re.compile(r"^\s*(.+?)\s*\|\s*([1-5])\s*\|\s*(\S*)\s*$")


# ---------- 上下文注入 ----------

def build_memory_context(user_id: Optional[int] = None) -> str:
    """长期记忆 → 系统提示词片段（过滤过期，按重要度降序，空则返回空串）。"""
    from app.utils.scoping import current_user_id

    uid = user_id if user_id is not None else current_user_id()
    rows = (Memory.query.filter(
        Memory.deleted_at.is_(None),
        Memory.user_id == uid,
        (Memory.expires_at.is_(None)) | (Memory.expires_at > utcnow()),
    ).order_by(Memory.importance.desc(), Memory.updated_at.desc())
        .limit(MEMORY_LIMIT).all())
    if not rows:
        return ""
    lines = [f"- {m.content[:MEMORY_TRUNCATE]}" for m in rows]
    return ("以下是关于用户的长期记忆（已知事实与偏好，可能过时，与当前对话冲突时以当前对话为准）：\n"
            + "\n".join(lines))


def delete_expired(user_id: Optional[int] = None) -> int:
    """软删过期的自动记忆，返回条数。"""
    from app.utils.scoping import current_user_id

    uid = user_id if user_id is not None else current_user_id()
    now = utcnow()
    q = Memory.query.filter(
        Memory.deleted_at.is_(None), Memory.expires_at.isnot(None),
        Memory.expires_at < now, Memory.user_id == uid)
    count = q.count()
    q.update({"deleted_at": now}, synchronize_session=False)
    db.session.commit()
    return count


# ---------- 会话摘要 ----------

def consolidate_conversation(conv: Conversation, user) -> bool:
    """已声明模型容量时按 token 预算梳理；旧配置保留 >12 条策略。"""
    from app.services import context_service

    with context_service.conversation_lock(conv.id):
        status = context_service.context_status(conv, user)
        if not status["legacy_context_policy"]:
            return context_service.maybe_compact_conversation(conv, user)["changed"]
        if status["active_messages"] <= CONSOLIDATE_MIN:
            return False
        return context_service.compact_conversation(conv, user, force=True)["changed"]


# ---------- 长期记忆抽取 ----------

def extract_memories(user) -> int:
    """跨对话抽取长期记忆，替换旧的自动记忆，返回新增条数。"""
    from app.ai.llm import LLMClient, LLMError
    from app.ai.prompts import build_system_prompt, extract_memories_prompt

    llm = LLMClient()
    if not llm.is_configured:
        return 0

    chunks = []
    convs = (Conversation.query.filter(Conversation.user_id == user.id)
             .order_by(Conversation.updated_at.desc()).limit(30).all())
    for conv in convs:
        if conv.summary:
            chunks.append(conv.summary)
        else:
            msgs = [m for m in conv.messages
                    if m.role in ("user", "assistant") and m.content][-20:]
            if msgs:
                chunks.append("\n".join(
                    f"{'用户' if m.role == 'user' else '助手'}: {m.content[:200]}" for m in msgs))
    if not chunks:
        return 0
    text = "\n\n".join(chunks)[:12000]

    try:
        content, _ = llm.chat([
            {"role": "system", "content": build_system_prompt(user)},
            {"role": "user", "content": extract_memories_prompt(text)},
        ])
    except LLMError as e:
        logger.warning("长期记忆抽取失败：%s", e)
        return 0

    lines = [ln.strip().lstrip("-•* ").strip()
             for ln in (content or "").splitlines()
             if ln.strip().lstrip("-•* ").strip()]
    tz = get_tz(getattr(user, "timezone", None) or "Asia/Shanghai")
    # 先解析有效记忆；全部无效时保留旧记忆（防模型异常输出清空已有记忆）
    parsed: list[tuple[str, int, Optional[datetime]]] = []
    for ln in lines[:MEMORY_LIMIT]:
        m = _MEM_LINE_RE.match(ln)
        if m:
            try:
                importance = int(m.group(2))
            except (TypeError, ValueError):
                importance = 3
            text, exp = m.group(1).strip(), m.group(3)
        else:
            text, importance, exp = ln, 3, ""
        if len(text) < 3:
            continue
        expires = None
        if exp and exp not in ("无", "-", "none", "null", "None"):
            parsed_exp = parse_local(exp, tz)
            if parsed_exp is not None:
                expires = parsed_exp
        parsed.append((text[:500], importance, expires))
    if not parsed:
        logger.warning("长期记忆抽取无有效结果，保留原有记忆")
        return 0
    # 替换旧的自动记忆（仅该用户）
    Memory.query.filter(Memory.source == SOURCE_AUTO, Memory.user_id == user.id).update(
        {"deleted_at": utcnow()}, synchronize_session=False)
    added = 0
    for text, importance, expires in parsed:
        db.session.add(Memory(user_id=user.id, content=text, source=SOURCE_AUTO,
                              importance=importance, expires_at=expires))
        added += 1
    db.session.commit()
    return added


def consolidate_all(user) -> dict:
    """梳理全部：清理过期记忆 + 压缩有消息的长对话 + 抽取该用户长期记忆。"""
    report = {"summarized": 0, "memories": 0, "expired": 0}
    report["expired"] = delete_expired(user.id)
    # A few long messages can exhaust a small declared window. Select every
    # conversation with usable history and leave threshold decisions to the
    # shared context policy rather than filtering by message count here.
    busy_ids = [r[0] for r in db.session.query(Message.conversation_id)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .filter(Conversation.user_id == user.id,
                        Message.role.in_(("user", "assistant")), Message.content != "")
                .distinct().all()]
    for conv_id in busy_ids:
        conv = db.session.get(Conversation, conv_id)
        if conv is None:
            continue
        if consolidate_conversation(conv, user):
            report["summarized"] += 1
            # RAG：把会话摘要入向量索引（未配置嵌入服务时静默降级）
            try:
                from app.services import rag_service

                rag_service.index_text("conversation", conv.id, conv.summary or "")
            except Exception:  # noqa: BLE001
                logger.exception("会话向量索引失败 conv=%s", conv.id)
    report["memories"] = extract_memories(user)
    return report


@register_action(
    "context_consolidation",
    description="定时梳理上下文：长对话压缩为摘要 + 抽取全局长期记忆（可到「记忆」页或任务页手动执行）",
)
def run_consolidation(user, params: Optional[dict] = None) -> None:
    """调度动作：上下文梳理（静默后台任务，结果记日志）。"""
    report = consolidate_all(user)
    logger.info("上下文梳理完成：压缩 %d 个会话，抽取 %d 条记忆",
                report["summarized"], report["memories"])


# ---------- 手动记忆 ----------

def remember(user_id: int, content: str, importance: int = 3, expires_at=None) -> Memory:
    """手动记录一条长期记忆（source=manual，不被自动梳理覆盖）。

    importance 1-5；expires_at 为 naive UTC datetime 或 None。
    """
    content = (content or "").strip()
    if not content:
        raise ValueError("记忆内容不能为空")
    try:
        importance = int(importance)
    except (TypeError, ValueError):
        importance = 3
    importance = max(1, min(5, importance))
    memory = Memory(user_id=user_id, content=content[:1000], source=SOURCE_MANUAL,
                    importance=importance, expires_at=expires_at)
    db.session.add(memory)
    db.session.commit()
    return memory


def list_memories(user_id: int, limit: int = 100) -> list[Memory]:
    return (Memory.query.filter(Memory.deleted_at.is_(None), Memory.user_id == user_id)
            .order_by(Memory.updated_at.desc()).limit(limit).all())


def get_memory(memory_id, user_id: Optional[int] = None) -> Optional[Memory]:
    try:
        memory_id = int(memory_id)
    except (TypeError, ValueError):
        return None
    memory = db.session.get(Memory, memory_id)
    if memory is None or memory.deleted_at is not None:
        return None
    if user_id is not None and memory.user_id != user_id:
        return None
    return memory


def soft_delete_memory(memory: Memory) -> None:
    memory.deleted_at = utcnow()
    db.session.commit()
