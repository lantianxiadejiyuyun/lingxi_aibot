"""笔记服务：增删改查与 AI 检索。

- 删除一律软删除（deleted_at = utcnow()），查询默认过滤。
- tags 为 JSON list，数据量小，关键词过滤（含标签）在 Python 侧完成。
"""
from __future__ import annotations

from typing import Optional

from app.extensions import db
from app.models.note import Note
from app.utils.timeutil import utcnow


def _normalize_tags(tags) -> list[str]:
    """标签归一化：接受 list 或逗号分隔字符串，去空去重保序。"""
    if tags is None:
        return []
    if isinstance(tags, str):
        raw = [t.strip() for t in tags.split(",")]
    else:
        raw = [str(t).strip() for t in tags]
    seen: set[str] = set()
    result: list[str] = []
    for t in raw:
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


def list_notes(user_id: int, q: Optional[str] = None, include_deleted: bool = False,
               limit: int = 100) -> list[Note]:
    """列出某用户笔记，默认过滤软删除；q 模糊匹配标题/内容/标签；按 updated_at 倒序。"""
    query = Note.query.filter(Note.user_id == user_id)
    if not include_deleted:
        query = query.filter(Note.deleted_at.is_(None))
    notes = query.order_by(Note.updated_at.desc()).all()
    if q:
        q = q.strip().lower()
        if q:
            notes = [
                n for n in notes
                if q in n.title.lower()
                or q in n.content.lower()
                or any(q in str(t).lower() for t in (n.tags or []))
            ]
    return notes[:limit]


def create_note(user_id: int, title: str, content: str = "", tags=None) -> Note:
    """新建笔记并落库。标题必填，为空抛 ValueError。"""
    title = (title or "").strip()
    if not title:
        raise ValueError("标题不能为空")
    note = Note(user_id=user_id, title=title, content=content or "", tags=_normalize_tags(tags))
    db.session.add(note)
    db.session.commit()
    _rag_index(note)
    return note


def _rag_index(note: Note, delete: bool = False) -> None:
    """RAG 向量索引（未配置嵌入服务时静默降级）。"""
    from app.services import rag_service

    if delete:
        rag_service.delete_index("note", note.id)
    else:
        rag_service.index_text("note", note.id, f"{note.title}\n{note.content}")


def update_note(note: Note, **fields) -> Note:
    """更新笔记，允许 title / content / tags；值为 None 的字段不修改。"""
    if "title" in fields and fields["title"] is not None:
        title = str(fields["title"]).strip()
        if not title:
            raise ValueError("标题不能为空")
        note.title = title
    if "content" in fields and fields["content"] is not None:
        note.content = str(fields["content"])
    if "tags" in fields and fields["tags"] is not None:
        note.tags = _normalize_tags(fields["tags"])
    db.session.commit()
    _rag_index(note)
    return note


def soft_delete_note(note: Note) -> None:
    """软删除笔记。"""
    note.deleted_at = utcnow()
    db.session.commit()
    _rag_index(note, delete=True)


def search_notes(user_id: int, q: str, limit: int = 10) -> list[dict]:
    """关键词检索笔记，返回 [{id, title, content_preview, tags}]，供 AI 引用。"""
    notes = list_notes(user_id, q=q, include_deleted=False, limit=limit)
    return [
        {
            "id": n.id,
            "title": n.title,
            "content_preview": (n.content or "").strip()[:120],
            "tags": list(n.tags or []),
        }
        for n in notes
    ]
