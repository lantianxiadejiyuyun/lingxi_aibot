"""联网搜索服务：可插拔搜索提供方。

- 默认 bing：免 key 开箱即用，国内网络可用（解析 HTML 结果页）
- duckduckgo：免 key（部分网络不可达，国外部署可用）
- searxng：自托管实例 JSON 接口（SEARXNG_BASE_URL）
- serper：Google SERP API（SERPER_API_KEY，https://serper.dev）
- tavily：AI 调研 API（TAVILY_API_KEY，https://tavily.com）
- fetch_page：抓取网页正文文本（供调查/阅读）

配置优先级：settings 表 > .env > 默认值。
"""
from __future__ import annotations

import html as _html
import logging
import re
import urllib.parse

import requests

from app.services.settings_service import get_setting_from

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 15
PROVIDERS = ("bing", "duckduckgo", "searxng", "serper", "tavily")


class SearchError(Exception):
    """搜索/抓取失败（信息回传模型或前端）。"""


def _cfg() -> dict:
    return {
        "provider": str(get_setting_from("search_provider", "SEARCH_PROVIDER", "bing") or "").strip().lower(),
        "searxng_base_url": str(get_setting_from("searxng_base_url", "SEARXNG_BASE_URL", "") or "").strip().rstrip("/"),
        "serper_api_key": str(get_setting_from("serper_api_key", "SERPER_API_KEY", "") or "").strip(),
        "tavily_api_key": str(get_setting_from("tavily_api_key", "TAVILY_API_KEY", "") or "").strip(),
    }


def is_configured() -> bool:
    """当前提供方是否可用（bing/duckduckgo 免 key 恒可用）。"""
    c = _cfg()
    if c["provider"] == "searxng":
        return bool(c["searxng_base_url"])
    if c["provider"] == "serper":
        return bool(c["serper_api_key"])
    if c["provider"] == "tavily":
        return bool(c["tavily_api_key"])
    return c["provider"] in ("bing", "duckduckgo")


def _snippet(text: str, limit: int = 220) -> str:
    text = _html.unescape(text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _search_bing(query: str, limit: int) -> list[dict]:
    resp = requests.get("https://www.bing.com/search",
                        params={"q": query, "count": limit},
                        headers={"User-Agent": UA}, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise SearchError(f"Bing 请求失败 HTTP {resp.status_code}")
    page = resp.text
    results: list[dict] = []
    for m in re.finditer(r'<li class="b_algo".*?</li>', page, re.S):
        if len(results) >= limit:
            break
        block = m.group(0)
        hm = re.search(r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>', block)
        if not hm:
            continue
        url = _html.unescape(hm.group(1))
        title = _html.unescape(re.sub(r"<[^>]+>", "", hm.group(2))).strip()
        sm = re.search(r"<p[^>]*>([\s\S]*?)</p>", block)
        snippet = _snippet(re.sub(r"<[^>]+>", "", sm.group(1))) if sm else ""
        results.append({"title": title or url, "url": url, "snippet": snippet})
    return results


def _search_duckduckgo(query: str, limit: int) -> list[dict]:
    resp = requests.get("https://html.duckduckgo.com/html/", params={"q": query},
                        headers={"User-Agent": UA}, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise SearchError(f"DuckDuckGo 请求失败 HTTP {resp.status_code}")
    page = resp.text
    results: list[dict] = []
    # 结果块：<a ... class="result__a" href="...">标题</a> 与 <a class="result__snippet">
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>', page):
        if len(results) >= limit:
            break
        raw_url = _html.unescape(m.group(1))
        url = raw_url
        if "uddg=" in raw_url:
            url = urllib.parse.unquote(
                urllib.parse.parse_qs(urllib.parse.urlparse(raw_url).query).get("uddg", [""])[0])
        title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        results.append({"title": title or url, "url": url, "snippet": ""})
    # 补充摘要（按顺序匹配 result__snippet）
    snips = re.findall(r'class="result__snippet"[^>]*>([\s\S]*?)</a>', page)
    for i, s in enumerate(snips[:limit]):
        if i < len(results):
            results[i]["snippet"] = _snippet(re.sub(r"<[^>]+>", "", s))
    return results


def _search_searxng(query: str, limit: int) -> list[dict]:
    c = _cfg()
    if not c["searxng_base_url"]:
        raise SearchError("未配置 SearXNG 实例地址（SEARXNG_BASE_URL）")
    resp = requests.get(f"{c['searxng_base_url']}/search",
                        params={"q": query, "format": "json"}, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise SearchError(f"SearXNG 请求失败 HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise SearchError("SearXNG 返回非 JSON（需开启 JSON 输出格式）") from None
    return [{"title": r.get("title") or "", "url": r.get("url") or "",
             "snippet": _snippet(r.get("content"))}
            for r in (data.get("results") or [])[:limit] if r.get("url")]


def _search_serper(query: str, limit: int) -> list[dict]:
    c = _cfg()
    if not c["serper_api_key"]:
        raise SearchError("未配置 Serper API Key")
    resp = requests.post("https://google.serper.dev/search",
                         json={"q": query, "num": limit},
                         headers={"X-API-KEY": c["serper_api_key"],
                                  "Content-Type": "application/json"}, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise SearchError(f"Serper 请求失败 HTTP {resp.status_code}: {resp.text[:150]}")
    try:
        data = resp.json()
    except ValueError:
        raise SearchError("Serper 返回非 JSON") from None
    return [{"title": r.get("title") or "", "url": r.get("link") or "",
             "snippet": _snippet(r.get("snippet"))}
            for r in (data.get("organic") or [])[:limit] if r.get("link")]


def _search_tavily(query: str, limit: int) -> list[dict]:
    c = _cfg()
    if not c["tavily_api_key"]:
        raise SearchError("未配置 Tavily API Key")
    resp = requests.post("https://api.tavily.com/search",
                         json={"api_key": c["tavily_api_key"], "query": query,
                               "max_results": limit}, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise SearchError(f"Tavily 请求失败 HTTP {resp.status_code}: {resp.text[:150]}")
    try:
        data = resp.json()
    except ValueError:
        raise SearchError("Tavily 返回非 JSON") from None
    return [{"title": r.get("title") or "", "url": r.get("url") or "",
             "snippet": _snippet(r.get("content"))}
            for r in (data.get("results") or [])[:limit] if r.get("url")]


def search(query: str, limit: int = 5) -> list[dict]:
    """按配置的提供方搜索，返回 [{title, url, snippet}]。"""
    query = (query or "").strip()
    if not query:
        raise SearchError("搜索关键词不能为空")
    limit = max(1, min(int(limit or 5), 10))
    provider = _cfg()["provider"]
    try:
        if provider == "bing":
            return _search_bing(query, limit)
        if provider == "searxng":
            return _search_searxng(query, limit)
        if provider == "serper":
            return _search_serper(query, limit)
        if provider == "tavily":
            return _search_tavily(query, limit)
        if provider == "duckduckgo":
            return _search_duckduckgo(query, limit)
    except requests.RequestException as e:
        raise SearchError(f"搜索网络异常：{str(e)[:120]}") from e
    raise SearchError(f"未知搜索提供方：{provider}")


def fetch_page(url: str, max_chars: int = 6000) -> str:
    """抓取网页正文文本（去 script/style/标签），供 AI 阅读。仅允许公网地址（防 SSRF）。"""
    from app.utils.urlsafety import requests_public_hook, validate_public_url

    url = (url or "").strip()
    try:
        url = validate_public_url(url)
    except ValueError as e:
        raise SearchError(f"拒绝访问：{e}") from e
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT,
                            hooks={"response": requests_public_hook})
    except (requests.RequestException, ValueError) as e:
        raise SearchError(f"页面抓取网络异常：{str(e)[:120]}") from e
    if resp.status_code >= 400:
        raise SearchError(f"页面抓取失败 HTTP {resp.status_code}")
    text = resp.text
    text = re.sub(r"(?is)<script[\s\S]*?</script>", " ", text)
    text = re.sub(r"(?is)<style[\s\S]*?</style>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]
