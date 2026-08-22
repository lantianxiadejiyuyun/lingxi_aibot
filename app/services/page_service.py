"""网页服务：页面 CRUD、slug 生成与校验、复制、软删除、公开访问解析。

- slug 规则：小写字母/数字/连字符，2-64 位，全局唯一（含软删除行，删除时重写 slug 释放）
- content 为完整 HTML 源码（自由页面，渲染时不转义）
- 软删除：deleted_at 置当前时间，并把 slug 改写为 <原>-d<id> 释放原地址
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

from app.extensions import db
from app.models.webpage import WebPage
from app.services.settings_service import get_setting_from
from app.utils.timeutil import utcnow

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
# 域名（DNS 名，非 IP）：标签不以连字符开头/结尾，点分隔
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def _normalize_domain(value) -> str:
    """规范化域名：小写、剥协议/端口/路径、去末尾点；非法返回空串。"""
    value = (value or "").strip().lower()
    value = re.sub(r"^https?://", "", value).split("/")[0].split(":")[0].strip().rstrip(".")
    if not _DOMAIN_RE.match(value):
        return ""
    return value

# 默认空白页模板：新建/复制时作为初始内容。
# 占位符 {title} 用 replace 替换（模板内含 CSS 花括号，不能用 str.format）
DEFAULT_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
           max-width: 720px; margin: 60px auto; padding: 0 20px; line-height: 1.8; color: #333; }
    h1 { font-size: 2rem; margin-bottom: 0.4em; }
    .hint { color: #999; font-size: 0.9rem; border-top: 1px solid #eee; margin-top: 40px; padding-top: 12px; }
  </style>
</head>
<body>
  <h1>{title}</h1>
  <p>这是一个新页面，点击「编辑」修改内容，或用 AI 助手生成。</p>
  <p class="hint">由 灵犀 网页生成器创建</p>
</body>
</html>
"""


def default_template(title: str = "新页面") -> str:
    """默认空白页模板（替换标题占位符）。"""
    return DEFAULT_TEMPLATE.replace("{title}", title)


def validate_slug(slug: str) -> str:
    """校验并返回规范化 slug，非法抛 ValueError。"""
    slug = (slug or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise ValueError("slug 需为 2-64 位小写字母/数字/连字符，且以字母或数字开头")
    return slug


def generate_slug(title: str = "") -> str:
    """自动生成唯一 slug：p + 8 位随机 hex（不依赖标题语言）。"""
    while True:
        slug = "p" + uuid.uuid4().hex[:8]
        if not slug_exists(slug):
            return slug


def slug_exists(slug: str) -> bool:
    return WebPage.query.filter_by(slug=slug).first() is not None


def get_page(page_id, user_id: Optional[int] = None) -> Optional[WebPage]:
    """按 id 取未删除页面，不存在、已删除或非本用户返回 None。"""
    try:
        page_id = int(page_id)
    except (TypeError, ValueError):
        return None
    page = db.session.get(WebPage, page_id)
    if page is None or page.deleted_at is not None:
        return None
    if user_id is not None and page.user_id != user_id:
        return None
    return page


def get_page_by_slug(slug: str) -> Optional[WebPage]:
    """按 slug 取未删除页面。"""
    if not slug:
        return None
    return WebPage.query.filter(
        WebPage.slug == slug.strip().lower(),
        WebPage.deleted_at.is_(None),
    ).first()


def _clean_html(content: str) -> str:
    """清洗 AI 生成的 HTML：去掉 ```html / ``` 代码围栏。"""
    content = (content or "").strip()
    if content.startswith("```"):
        # 去掉首行围栏（```html 或 ```）与末行 ```
        lines = content.split("\n")
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        content = "\n".join(lines).strip()
    return content


def create_page(user_id: int, title: str, content: str = "", slug: Optional[str] = None,
                description: str = "", is_public: bool = True,
                enabled: bool = True) -> WebPage:
    """创建页面并落库。slug 为空则自动生成；content 为空则使用默认模板。"""
    title = (title or "").strip()
    if not title:
        raise ValueError("页面标题不能为空")
    if slug:
        slug = validate_slug(slug)
        if slug_exists(slug):
            raise ValueError(f"slug 已被占用：{slug}")
    else:
        slug = generate_slug(title)

    content = _clean_html(content) if content else default_template(title)
    page = WebPage(
        user_id=user_id,
        title=title,
        slug=slug,
        description=(description or "").strip()[:500],
        content=content,
        is_public=bool(is_public),
        enabled=bool(enabled),
    )
    db.session.add(page)
    db.session.commit()
    _rag_index(page)
    return page


def _rag_index(page: WebPage, delete: bool = False) -> None:
    """RAG 向量索引（未配置嵌入服务时静默降级）。"""
    from app.services import rag_service

    if delete:
        rag_service.delete_index("webpage", page.id)
    else:
        rag_service.index_text(
            "webpage", page.id,
            f"{page.title}\n{page.description}\n{(page.content or '')[:3000]}")


def update_page(page: WebPage, title: Optional[str] = None, content: Optional[str] = None,
                slug: Optional[str] = None, description: Optional[str] = None,
                is_public: Optional[bool] = None, enabled: Optional[bool] = None) -> WebPage:
    """更新页面字段，传入 None 的字段不改。改 slug 时校验唯一性。"""
    if title is not None:
        title = (title or "").strip()
        if not title:
            raise ValueError("页面标题不能为空")
        page.title = title
    if slug is not None:
        new_slug = validate_slug(slug)
        if new_slug != page.slug and slug_exists(new_slug):
            raise ValueError(f"slug 已被占用：{new_slug}")
        page.slug = new_slug
    if content is not None:
        page.content = _clean_html(content) if (content or "").strip() else content
    if description is not None:
        page.description = (description or "").strip()[:500]
    if is_public is not None:
        page.is_public = bool(is_public)
    if enabled is not None:
        page.enabled = bool(enabled)
    db.session.commit()
    _rag_index(page)
    return page


def soft_delete_page(page: WebPage) -> None:
    """软删除：置 deleted_at，并改写 slug（<原>-d<id>）释放原地址（防冲突循环加后缀）。"""
    page.deleted_at = utcnow()
    base = f"{page.slug[:48]}-d{page.id}"
    freed = base
    i = 1
    while slug_exists(freed):
        suffix = f"-{i}"
        freed = base[: 64 - len(suffix)] + suffix
        i += 1
    page.slug = freed  # 唯一约束安全：改写后的 slug 不与现有任何行冲突
    db.session.commit()
    _rag_index(page, delete=True)


def duplicate_page(user_id: int, page: WebPage) -> WebPage:
    """复制页面为新页面（slug 自动生成，标题加「副本」后缀，内容不变）。"""
    return create_page(
        user_id,
        title=f"{page.title}（副本）",
        content=page.content,
        description=page.description,
        is_public=page.is_public,
        enabled=page.enabled,
    )


def list_pages(user_id: int, include_deleted: bool = False) -> list[WebPage]:
    """某用户的全部页面，未删除的排前面（按更新时间倒序）。"""
    query = WebPage.query.filter(WebPage.user_id == user_id)
    if not include_deleted:
        query = query.filter(WebPage.deleted_at.is_(None))
    return query.order_by(WebPage.updated_at.desc()).all()


def visible_by_slug(slug: str) -> Optional[WebPage]:
    """公开访问解析：未删除且 enabled 的页面；is_public 由调用方结合登录态判断。"""
    page = get_page_by_slug(slug)
    if page is None or not page.enabled:
        return None
    return page


def page_port_configured() -> int:
    """独立网页 HTTP 端口；0 表示不另开端口（走后台 /p/<slug>）。"""
    raw = get_setting_from("page_port", "PAGE_PORT", 0, user_id=0)
    try:
        port = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    if 1 <= port <= 65535:
        return port
    return 0


def page_access_host() -> str:
    """生成网页链接用的主机名/IP：设置 PAGE_HOST，否则用本机出口 IPv4，再退回请求 Host。"""
    host = str(get_setting_from("page_host", "PAGE_HOST", "", user_id=0) or "").strip()
    host = host.replace("http://", "").replace("https://", "").split("/")[0].split(":")[0]
    if host:
        return host
    try:
        from app.utils.netinfo import diagnose_page_reachability

        ip = (diagnose_page_reachability().get("local_ipv4") or "").strip()
        if ip and ip not in ("127.0.0.1", "::1"):
            return ip
    except Exception:  # noqa: BLE001
        pass
    try:
        from flask import has_request_context, request

        if has_request_context() and request.host:
            h = request.host
            if h.startswith("["):
                return h.split("]", 1)[0].lstrip("[")
            return h.split(":")[0]
    except Exception:  # noqa: BLE001
        pass
    return "127.0.0.1"


def page_site_base_url() -> str:
    """独立网页站点根 URL（http，不配证书）。未开端口返回空串。"""
    port = page_port_configured()
    if not port:
        return ""
    host = page_access_host()
    if port == 80:
        return f"http://{host}"
    return f"http://{host}:{port}"


def page_public_url(page: WebPage) -> str:
    """页面访问地址。

    优先：独立网页端口 → http://主机:端口/<slug>（无需 HTTPS/域名）
    其次：PAGE_DOMAIN / ADMIN_DOMAIN 双域名
    否则：当前后台 /p/<slug>
    """
    from flask import has_request_context, request

    port_base = page_site_base_url()
    if port_base and page.is_public:
        return f"{port_base}/{page.slug}"

    page_domain = page_domain_configured()
    admin_domain = admin_domain_configured()
    entry = str(get_setting_from("admin_entry", "ADMIN_ENTRY", "", user_id=0) or "").strip().strip("/")
    prefix = f"/{entry}" if entry else ""

    if page.is_public and page_domain:
        return f"https://{page_domain}/{page.slug}"
    if admin_domain:
        return f"https://{admin_domain}{prefix}/p/{page.slug}"
    if page_domain:
        return f"https://{page_domain}{prefix}/p/{page.slug}"
    if has_request_context():
        return f"{request.host_url.rstrip('/')}{prefix}/p/{page.slug}"
    return f"{prefix}/p/{page.slug}"


def page_domain_configured() -> str:
    """配置的网页域名（未配置或与后台域名相等时返回空串，避免网页路由接管后台）。全局设置。"""
    page = _normalize_domain(get_setting_from("page_domain", "PAGE_DOMAIN", "", user_id=0) or "")
    admin = admin_domain_configured()
    if page and page == admin:
        return ""
    return page


def admin_domain_configured() -> str:
    """配置的后台域名（未配置返回空串）。全局设置。"""
    return _normalize_domain(get_setting_from("admin_domain", "ADMIN_DOMAIN", "", user_id=0) or "")
