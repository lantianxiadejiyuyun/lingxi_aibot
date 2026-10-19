"""Bounded read-only discovery. Search snippets are evidence, never instructions."""
from __future__ import annotations

import hashlib
import html
import re
from urllib.parse import urlsplit

from app.services import web_search_service


def _plain(value, limit):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))).split())[:limit]


def canonical_bilibili_url(value: str) -> str | None:
    """Only actual video URLs; remove tracking params and never fetch the result."""
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or parts.username or parts.password
                or parts.hostname not in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
                or parts.port not in {None, 80, 443}):
            return None
        match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10}|av[0-9]{1,20})/?", parts.path)
        if match:
            return "https://www.bilibili.com/video/" + match.group(1)
    except (ValueError, TypeError):
        pass
    return None


def discover(config: dict, now) -> list[dict]:
    """At most one search and three public results per tick, no cookies/accounts."""
    interests = config.get("interests") or []
    if not config.get("share_bilibili") or not interests:
        return []
    # Rotate predictably without replaying a whole interest list on every tick.
    index = (now.toordinal() * 24 + now.hour) % len(interests)
    keyword = interests[index]
    results = web_search_service.search(f"site:bilibili.com/video/ {keyword}", limit=3)
    sources = []
    seen = set()
    for item in results[:3]:
        url = canonical_bilibili_url(item.get("url", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        sources.append({
            "id": "bili:" + hashlib.sha256(url.encode()).hexdigest()[:24],
            "kind": "bilibili_search", "title": _plain(item.get("title"), 160),
            "url": url, "snippet": _plain(item.get("snippet"), 300),
            "query": keyword, "observed_at": now.isoformat() + "Z",
        })
    return sources
