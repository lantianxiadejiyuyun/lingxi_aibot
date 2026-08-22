"""记忆服务：会话摘要 + 全局长期记忆 + 上下文注入 + 定时梳理动作。

- 会话摘要：长对话（>12 条）压缩为 conv.summary，保留最近 8 条原文
- 长期记忆：跨对话抽取事实/偏好存 memories 表（source=auto 每次梳理整体替换，
  source=manual 由用户/AI 手动记录、不被自动覆盖）；每条带重要度 1-5 与过期时间
- 上下文注入：build_messages 注入 全局记忆（过滤过期、按重要度排序）+ 会话摘要 + 最近消息
- 定时动作：context_consolidation（内置任务，默认每日 04:00）
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from sqlalchemy import func

from app.extensions import db
from app.models.conversation import Conversation, Message
from app.models.memory import SOURCE_AUTO, SOURCE_MANUAL, Memory
from app.models.user import User
from app.scheduler import register_action
from app.utils.timeutil import get_tz, parse_local, utcnow

logger = logging.getLogger(__name__)

KEEP_RECENT = 8          # 有摘要时保留的最近消息条数
CONSOLIDATE_MIN = 12     # 超过该条数才压缩
MEMORY_LIMIT = 50        # 注入上下文的最大记忆条数
MEMORY_TRUNCATE = 300    # 单条记忆注入时截断长度

# 抽取输出格式：内容 | 重要度1-5 | 过期时间（YYYY-MM-DD，无则 无）
_MEM_LINE_RE = re.compile(r"^\s*(.+?)\s*\|\s*([1-5])\s*\|\s*(\S*)\s*$")


# ---------- 上下文注入 ----------

def build_memory_context() -> str:
    """长期记忆 → 系统提示词片段（过滤过期，按重要度降序，空则返回空串）。"""
    rows = (Memory.query.filter(
        Memory.deleted_at.is_(None),
        (Memory.expires_at.is_(None)) | (Memory.expires_at > utcnow()),
    ).order_by(Memory.importance.desc(), Memory.updated_at.desc())
        .limit(MEMORY_LIMIT).all())
    if not rows:
        return ""
    lines = [f"- {m.content[:MEMORY_TRUNCATE]}" for m in rows]
    return ("以下是关于用户的长期记忆（已知事实与偏好，可能过时，与当前对话冲突时以当前对话为准）：\n"
            + "\n".join(lines))


def delete_expired() -> int:
    """软删过期的自动记忆，返回条数。"""
    now = utcnow()
    count = Memory.query.filter(
        Memory.deleted_at.is_(None), Memory.expires_at.isnot(None),
        Memory.expires_at < now).count()
    Memory.query.filter(
        Memory.deleted_at.is_(None), Memory.expires_at.isnot(None),
        Memory.expires_at < now).update({"deleted_at": now}, synchronize_session=False)
    db.session.commit()
    return count


# ---------- 会话摘要 ----------

def consolidate_conversation(conv: Conversation, user) -> bool:
    """把长对话压缩为摘要写入 conv.summary，保留最近 KEEP_RECENT 条原文。"""
    msgs = [m for m in conv.messages if m.role in ("user", "assistant") and m.content]
    if len(msgs) <= CONSOLIDATE_MIN:
        return False
    older = msgs[:-KEEP_RECENT]
    if not older:
        return False
    transcript = "\n".join(
        f"{'用户' if m.role == 'user' else '助手'}: {m.content[:500]}" for m in older)

    from app.ai.llm import LLMClient, LLMError
    from app.ai.prompts import build_system_prompt, summarize_prompt

    llm = LLMClient()
    if not llm.is_configured:
        return False
    try:
        summary, _ = llm.chat([
            {"role": "system", "content": build_system_prompt(user)},
            {"role": "user", "content": summarize_prompt(transcript)},
        ])
    except LLMError as e:
        logger.warning("会话 %s 摘要失败：%s", conv.id, e)
        return False
    summary = (summary or "").strip()
    if summary:
        conv.summary = summary
        db.session.commit()
        return True
    return False


# ---------- 长期记忆抽取 ----------

def extract_memories(user) -> int:
    """跨对话抽取长期记忆，替换旧的自动记忆，返回新增条数。"""
    from app.ai.llm import LLMClient, LLMError
    from app.ai.prompts import build_system_prompt, extract_memories_prompt

    llm = LLMClient()
    if not llm.is_configured:
        return 0

    chunks = []
    convs = (Conversation.query
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
    # 替换旧的自动记忆
    Memory.query.filter(Memory.source == SOURCE_AUTO).update(
        {"deleted_at": utcnow()}, synchronize_session=False)
    tz = get_tz(getattr(user, "timezone", None) or "Asia/Shanghai")
    added = 0
    for ln in lines[:MEMORY_LIMIT]:
        m = _MEM_LINE_RE.match(ln)
        if m:
            text, importance, exp = m.group(1).strip(), int(m.group(2)), m.group(3)
        else:
            text, importance, exp = ln, 3, ""
        if len(text) < 3:
            continue
        expires = None
        if exp and exp not in ("无", "-", "none", "null", "None"):
            parsed = parse_local(exp, tz)
            if parsed is not None:
                expires = parsed
        db.session.add(Memory(content=text[:500], source=SOURCE_AUTO,
                              importance=importance, expires_at=expires))
        added += 1
    db.session.commit()
    return added


def consolidate_all(user) -> dict:
    """梳理全部：清理过期记忆 + 压缩有消息的长对话 + 抽取全局长期记忆。"""
    report = {"summarized": 0, "memories": 0, "expired": 0}
    report["expired"] = delete_expired()
    busy_ids = [r[0] for r in db.session.query(Message.conversation_id)
                .group_by(Message.conversation_id)
                .having(func.count(Message.id) > KEEP_RECENT).all()]
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
def run_consolidation(params: Optional[dict] = None) -> None:
    """调度动作：上下文梳理（静默后台任务，结果记日志）。"""
    user = User.query.first()
    if user is None:
        return
    report = consolidate_all(user)
    logger.info("上下文梳理完成：压缩 %d 个会话，抽取 %d 条记忆",
                report["summarized"], report["memories"])


# ---------- 手动记忆 ----------

def remember(content: str, importance: int = 3, expires_at=None) -> Memory:
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
    memory = Memory(content=content[:1000], source=SOURCE_MANUAL,
                    importance=importance, expires_at=expires_at)
    db.session.add(memory)
    db.session.commit()
    return memory


def list_memories(limit: int = 100) -> list[Memory]:
    return (Memory.query.filter(Memory.deleted_at.is_(None))
            .order_by(Memory.updated_at.desc()).limit(limit).all())


def get_memory(memory_id) -> Optional[Memory]:
    try:
        memory_id = int(memory_id)
    except (TypeError, ValueError):
        return None
    memory = db.session.get(Memory, memory_id)
    if memory is None or memory.deleted_at is not None:
        return None
    return memory


def soft_delete_memory(memory: Memory) -> None:
    memory.deleted_at = utcnow()
    db.session.commit()
