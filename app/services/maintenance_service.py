"""每周自动整理：过期任务归档 / 重复笔记合并 / 网页坏链体检 / 旧通知清理。

调度动作 weekly_cleanup（内置任务，默认每周日 02:00），结果汇总推送 + 日志。
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Optional

import requests

from app.extensions import db
from app.models.notification import Notification
from app.models.note import Note
from app.models.task import STATUS_CANCELLED, STATUS_OPEN, Task
from app.models.user import User
from app.models.webpage import WebPage
from app.scheduler import register_action
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)

OVERDUE_DAYS = 7        # 到期超过 N 天的未完成任务自动归档
NOTIFY_KEEP_DAYS = 30   # 已读通知保留 N 天
LINK_SCAN_LIMIT = 30    # 坏链体检最多检查的链接数
_LINK_RE = re.compile(r'(?:href|src)="(https?://[^"#?]+[^"\s]*)"')


# ---------- 各清理项 ----------

def cleanup_overdue_tasks(user_id: int) -> int:
    """到期超过 7 天仍未完成的任务 → 自动归档（cancelled，保留记录）。"""
    now = utcnow()
    cutoff = now - timedelta(days=OVERDUE_DAYS)
    rows = Task.query.filter(
        Task.deleted_at.is_(None), Task.status == STATUS_OPEN, Task.user_id == user_id,
        Task.due_utc.isnot(None), Task.due_utc < cutoff,
    ).all()
    for t in rows:
        t.status = STATUS_CANCELLED
        t.completed_at = None
        t.notes = (t.notes + "\n[每周整理] 已超期未完成，自动归档").strip()
    if rows:
        db.session.commit()
    return len(rows)


def merge_duplicate_notes(user_id: int) -> int:
    """同标题笔记合并：内容并入最早一条，其余软删（同步维护 RAG 索引）。"""
    from app.services import rag_service

    notes = Note.query.filter(Note.deleted_at.is_(None), Note.user_id == user_id).order_by(Note.created_at).all()
    groups: dict[str, list[Note]] = {}
    for n in notes:
        groups.setdefault(n.title.strip(), []).append(n)
    merged = 0
    for title, items in groups.items():
        if len(items) < 2 or not title:
            continue
        keep, dupes = items[0], items[1:]
        parts = [keep.content or ""]
        for d in dupes:
            if d.content and d.content not in parts:
                parts.append(d.content)
        keep.content = "\n\n".join(parts)
        for d in dupes:
            d.deleted_at = utcnow()
            try:
                rag_service.delete_index("note", d.id, user_id)  # 软删副本清理索引
            except Exception:  # noqa: BLE001 —— RAG 不可用不影响合并
                logger.exception("清理笔记索引失败 note=%s", d.id)
        # 保留笔记内容合并后重建索引
        try:
            rag_service.index_text("note", keep.id, f"{keep.title}\n{keep.content}", user_id=user_id)
        except Exception:  # noqa: BLE001
            logger.exception("重建笔记索引失败 note=%s", keep.id)
        merged += len(dupes)
    if merged:
        db.session.commit()
    return merged


def _check_link(url: str) -> Optional[str]:
    """检查单个链接，返回错误信息（正常返回 None）。仅检查公网地址（防 SSRF）。"""
    from app.utils.urlsafety import safe_get, validate_public_url

    try:
        validate_public_url(url)
    except ValueError:
        return None  # 非公网链接：跳过不检查（防 SSRF，也不误报坏链）
    try:
        # safe_get：手动逐跳重定向，每跳请求前校验公网（杜绝 blind SSRF）
        resp = safe_get(url, timeout=6, stream=True)
    except ValueError:
        return None  # 重定向到非公网：忽略
    except requests.RequestException as e:
        return str(e)[:120]
    try:
        return None if resp.status_code < 400 else f"HTTP {resp.status_code}"
    finally:
        resp.close()


def check_page_links(user_id: int) -> list[str]:
    """扫描生成网页中的外链，返回坏链列表。"""
    broken: list[str] = []
    links: list[str] = []
    for page in WebPage.query.filter(WebPage.deleted_at.is_(None),
                                     WebPage.user_id == user_id).all():
        for m in _LINK_RE.finditer(page.content or ""):
            url = m.group(1)
            if url not in links:
                links.append(url)
        if len(links) >= LINK_SCAN_LIMIT:
            break
    if not links:
        return broken
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_check_link, links))
    for url, err in zip(links, results):
        if err:
            broken.append(f"{url} → {err}")
    return broken


def cleanup_old_notifications(user_id: int) -> int:
    """删除已读且超过 30 天的通知（与「清除已读」一致为硬删除）。"""
    cutoff = utcnow() - timedelta(days=NOTIFY_KEEP_DAYS)
    q = Notification.query.filter(
        Notification.read.is_(True), Notification.created_at < cutoff,
        Notification.user_id == user_id)
    count = q.count()
    q.delete(synchronize_session=False)
    db.session.commit()
    return count


def run_cleanup(user_id: int) -> dict:
    """执行全部清理项（按用户隔离），返回汇总报告。"""
    report = {
        "tasks_archived": cleanup_overdue_tasks(user_id),
        "notes_merged": merge_duplicate_notes(user_id),
        "broken_links": check_page_links(user_id),
        "notifications_removed": cleanup_old_notifications(user_id),
    }
    return report


@register_action("weekly_cleanup", description="每周自动整理：归档超期任务/合并重复笔记/坏链体检/清理旧通知")
def run_weekly_cleanup(user, params: Optional[dict] = None) -> None:
    """调度动作：每周整理，结果按场景渠道推送通知。"""
    from app.services.notify_service import notify_for

    report = run_cleanup(user.id)
    lines = [
        f"- 归档超期任务：{report['tasks_archived']} 项",
        f"- 合并重复笔记：{report['notes_merged']} 篇",
        f"- 网页坏链：{len(report['broken_links'])} 个",
        f"- 清理旧通知：{report['notifications_removed']} 条",
    ]
    if report["broken_links"]:
        lines.append("坏链明细：\n" + "\n".join("  " + b for b in report["broken_links"][:10]))
    notify_for("cleanup", "🧹 每周整理报告", "\n".join(lines), user_id=user.id)
    logger.info("每周整理完成：%s", report)
