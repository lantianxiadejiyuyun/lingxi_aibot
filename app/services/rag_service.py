"""RAG 语义检索：OpenAI 兼容 embeddings 接口 + 向量索引 + 余弦检索。

- 配置：embedding_base_url / embedding_api_key / embedding_model（settings 表 > .env，
  env 名 EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL）
- 未配置时所有函数安全降级（不抛错），供 note/page/memory 挂钩懒调用
- 向量一律归一化后存库，检索用点积（=余弦）
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from app.extensions import db
from app.models.embedding import Embedding
from app.services.settings_service import get_setting_from

logger = logging.getLogger(__name__)

MAX_TEXT = 8000
DEFAULT_K = 5
SOURCES = ("note", "webpage", "conversation")


class RagError(Exception):
    """嵌入/检索失败（信息回传调用方）。"""


def _cfg() -> dict:
    return {
        "base_url": str(get_setting_from("embedding_base_url", "EMBEDDING_BASE_URL", "") or "").strip().rstrip("/"),
        "api_key": str(get_setting_from("embedding_api_key", "EMBEDDING_API_KEY", "") or "").strip(),
        "model": str(get_setting_from("embedding_model", "EMBEDDING_MODEL", "") or "").strip(),
    }


def is_configured() -> bool:
    c = _cfg()
    return bool(c["base_url"] and c["api_key"] and c["model"])


def embed(text: str) -> list:
    """调嵌入接口，返回向量。未配置/失败抛 RagError。"""
    import requests

    c = _cfg()
    if not (c["base_url"] and c["api_key"] and c["model"]):
        raise RagError("语义检索未配置：请设置 EMBEDDING_BASE_URL / API_KEY / MODEL")
    resp = requests.post(
        f"{c['base_url']}/embeddings",
        json={"model": c["model"], "input": [text[:MAX_TEXT]]},
        headers={"Authorization": f"Bearer {c['api_key']}"}, timeout=60,
    )
    if resp.status_code != 200:
        raise RagError(f"嵌入接口失败 HTTP {resp.status_code}: {resp.text[:150]}")
    try:
        return resp.json()["data"][0]["embedding"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise RagError("嵌入接口返回格式异常") from None


def _norm(vec) -> list:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else list(vec)


def _cosine(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))  # 已归一化


# ---------- 索引 ----------

def _uid(user_id: Optional[int] = None) -> int:
    from app.utils.scoping import current_user_id

    return user_id if user_id is not None else current_user_id()


def index_text(source_type: str, source_id: int, text: str, model: Optional[str] = None,
               user_id: Optional[int] = None) -> None:
    """upsert 来源向量；未配置/异常一律静默降级。"""
    try:
        if not is_configured():
            return
        text = (text or "")[:MAX_TEXT]
        if not text.strip():
            return
        vec = embed(text)
        model = model or _cfg()["model"]
        uid = _uid(user_id)
        db.session.query(Embedding).filter_by(
            source_type=source_type, source_id=source_id, user_id=uid
        ).delete(synchronize_session=False)
        db.session.add(Embedding(source_type=source_type, source_id=source_id,
                                 user_id=uid, vector=_norm(vec), model=model))
        db.session.commit()
    except RagError as e:
        logger.warning("索引 %s:%s 失败：%s", source_type, source_id, e)
    except Exception:  # noqa: BLE001 —— 挂钩不得影响主流程
        logger.exception("索引 %s:%s 异常", source_type, source_id)


def delete_index(source_type: str, source_id: int, user_id: Optional[int] = None) -> None:
    try:
        uid = _uid(user_id)
        db.session.query(Embedding).filter_by(
            source_type=source_type, source_id=source_id, user_id=uid
        ).delete(synchronize_session=False)
        db.session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("删除索引 %s:%s 异常", source_type, source_id)


# ---------- 检索 ----------

def _source_text(source_type: str, source_id: int) -> str:
    """来源 → 检索用文本片段（软删除的来源返回空串，不泄露已删内容）。"""
    if source_type == "note":
        from app.models.note import Note

        n = db.session.get(Note, source_id)
        if n is None or n.deleted_at is not None:
            return ""
        return f"{n.title}\n{n.content}"
    if source_type == "webpage":
        from app.models.webpage import WebPage

        p = db.session.get(WebPage, source_id)
        if p is None or p.deleted_at is not None:
            return ""
        return f"{p.title}\n{p.description}\n{(p.content or '')[:3000]}"
    if source_type == "conversation":
        from app.models.conversation import Conversation

        conv = db.session.get(Conversation, source_id)
        if conv is None:
            return ""
        if conv.summary:
            return conv.summary
        msgs = [m.content for m in conv.messages
                if m.role in ("user", "assistant") and m.content][-10:]
        return "\n".join(msgs)
    return ""


def semantic_search(query: str, sources: Optional[list] = None,
                    k: int = DEFAULT_K, user_id: Optional[int] = None) -> list[dict]:
    """余弦检索，返回 [{source_type, source_id, score, text}]；未配置/无结果返回 []。"""
    try:
        if not is_configured() or not query or not str(query).strip():
            return []
        qv = _norm(embed(str(query).strip()[:MAX_TEXT]))
        uid = _uid(user_id)
        rows = Embedding.query.filter(Embedding.user_id == uid).all()
        if sources:
            rows = [r for r in rows if r.source_type in sources]
        scored = [( _cosine(qv, r.vector or []), r) for r in rows]
        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        for score, r in scored[:max(1, int(k))]:
            out.append({
                "source_type": r.source_type,
                "source_id": r.source_id,
                "score": round(score, 4),
                "text": _source_text(r.source_type, r.source_id)[:500],
            })
        return out
    except RagError as e:
        logger.warning("语义检索失败：%s", e)
        return []
    except Exception:  # noqa: BLE001
        logger.exception("语义检索异常")
        return []
