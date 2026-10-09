"""Public anime resource discovery; fetched pages are data, never instructions.

All page/RSS/torrent reads use the existing DNS-pinned, bounded safe_get. Download
engines are separate processes: their network egress policy remains independent.
"""
from __future__ import annotations

import base64
import hashlib
import html
from html.parser import HTMLParser
import re
import time
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import requests

from app.services import web_search_service
from app.utils.urlsafety import safe_get, validate_public_url

MAX_DOCUMENT = 4 * 1024 * 1024
MEDIA_EXTENSIONS = (".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts")


class SourceError(ValueError):
    """Safe, public error with no remote response body or credentials."""


def _http_url(url: str) -> str:
    value = html.unescape(str(url or "")).strip()
    if len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise SourceError("资源地址过长或包含控制字符")
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise ValueError()
        if parts.username is not None or parts.password is not None:
            raise ValueError()
        _ = parts.port
    except ValueError:
        raise SourceError("资源必须是无内嵌账号密码的 HTTP(S) 地址") from None
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, ""))


def validate_source_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise SourceError("来源配置须为对象")
    kind = str(config.get("kind") or "rss").lower()
    if kind not in ("rss", "search"):
        raise SourceError("来源类型应为 rss 或 search")
    result = {"kind": kind, "name": str(config.get("name") or ("RSS" if kind == "rss" else "网页搜索"))[:100],
              "enabled": config.get("enabled") is not False}
    if kind == "rss":
        try:
            result["url"] = validate_public_url(_http_url(str(config.get("url") or "")))
        except ValueError:
            raise SourceError("RSS 地址未通过公网安全校验") from None
    return result


def _source_label(url: str) -> str:
    """Feed URLs can contain API keys; attribution never includes query strings."""
    try:
        parts = urlsplit(str(url or ""))
        return urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))[:1000]
    except ValueError:
        return ""


def magnet_infohash(url: str) -> str:
    """Canonical v1 hex or v2 SHA-256, including base32 v1 magnets."""
    if not str(url).lower().startswith("magnet:?") or len(url) > 16384:
        raise SourceError("无效磁力链接")
    for xt in parse_qs(urlsplit(url).query).get("xt", []):
        if xt.lower().startswith("urn:btih:"):
            value = xt[9:]
            if re.fullmatch(r"[0-9a-fA-F]{40}", value):
                return value.lower()
            if re.fullmatch(r"[A-Za-z2-7]{32}", value):
                return base64.b32decode(value.upper()).hex()
    for xt in parse_qs(urlsplit(url).query).get("xt", []):
        if re.fullmatch(r"urn:btmh:1220[0-9a-fA-F]{64}", xt, re.I):
            return xt[13:].lower()
    raise SourceError("磁力链接缺少有效 BT infohash")


def torrent_infohash(data: bytes) -> str:
    """Read bounded bencode and hash the exact info bytes, not a re-encoding."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_DOCUMENT:
        raise SourceError("种子文件无效或超过 4 MB")
    info_slice = None
    nodes = 0

    def read(pos, depth=0):
        nonlocal info_slice, nodes
        nodes += 1
        if depth > 32 or nodes > 100000 or pos >= len(data):
            raise ValueError()
        start = pos
        token = data[pos:pos + 1]
        if token == b"i":
            end = data.index(b"e", pos + 1)
            raw = data[pos + 1:end]
            if not re.fullmatch(rb"-?(0|[1-9][0-9]*)", raw) or len(raw) > 20:
                raise ValueError()
            return int(raw), end + 1
        if token in (b"l", b"d"):
            result = [] if token == b"l" else {}
            pos += 1
            while pos < len(data) and data[pos:pos + 1] != b"e":
                key, pos = read(pos, depth + 1)
                if token == b"l":
                    result.append(key)
                else:
                    if not isinstance(key, bytes) or key in result:
                        raise ValueError()
                    value_start = pos
                    val, pos = read(pos, depth + 1)
                    result[key] = val
                    if depth == 0 and key == b"info":
                        info_slice = (value_start, pos)
            if pos >= len(data):
                raise ValueError()
            return result, pos + 1
        colon = data.index(b":", start)
        length = data[start:colon]
        if len(length) > 8 or not re.fullmatch(rb"0|[1-9][0-9]*", length):
            raise ValueError()
        end = colon + 1 + int(length)
        if end > len(data):
            raise ValueError()
        return data[colon + 1:end], end

    try:
        value, end = read(0)
        if end != len(data) or not isinstance(value, dict) or not info_slice:
            raise ValueError()
        info = value[b"info"]
        if not isinstance(info, dict) or not info.get(b"name"):
            raise ValueError()
        def safe_name(name):
            if not isinstance(name, bytes) or not name or name in (b".", b"..") or any(
                    token in name for token in (b"/", b"\\", b"\x00", b":")):
                raise ValueError()
        safe_name(info[b"name"])
        if b"name.utf-8" in info:
            safe_name(info[b"name.utf-8"])
        for file in info.get(b"files", []):
            if not isinstance(file, dict) or b"symlink path" in file or b"l" in file.get(b"attr", b""):
                raise ValueError()
            paths = file.get(b"path.utf-8", file.get(b"path"))
            if not isinstance(paths, list) or not paths:
                raise ValueError()
            for path in paths:
                safe_name(path)
        def check_tree(tree):
            if not isinstance(tree, dict):
                raise ValueError()
            for name, child in tree.items():
                if name == b"":
                    if not isinstance(child, dict) or b"symlink path" in child or b"l" in child.get(b"attr", b""):
                        raise ValueError()
                else:
                    safe_name(name)
                    check_tree(child)
        if b"file tree" in info:
            check_tree(info[b"file tree"])
        if b"symlink path" in info or b"l" in info.get(b"attr", b""):
            raise ValueError()
        # A hybrid torrent uses its v1 identity; pure v2 uses its SHA-256 identity.
        raw = data[slice(*info_slice)]
        return (hashlib.sha256(raw) if info.get(b"meta version") == 2 and b"pieces" not in info
                else hashlib.sha1(raw)).hexdigest()
    except (ValueError, IndexError, KeyError, TypeError):
        raise SourceError("种子文件格式无效") from None


def fetch_torrent(url: str) -> bytes:
    response = _fetch(_http_url(url))
    data = response.content
    torrent_infohash(data)
    return data


def _fetch(url: str):
    try:
        response = safe_get(_http_url(url), timeout=15, max_response_bytes=MAX_DOCUMENT,
                            headers={"User-Agent": web_search_service.UA})
        if response.status_code >= 400:
            raise SourceError(f"资源页面请求失败 HTTP {response.status_code}")
        return response
    except (requests.RequestException, ValueError) as exc:
        if isinstance(exc, SourceError):
            raise
        raise SourceError("资源不可访问，或地址未通过公网安全校验") from None


def make_candidate(url: str, title: str = "", source_url: str = "", kind: str = "") -> dict:
    value = html.unescape(str(url or "")).strip()
    if value.lower().startswith("magnet:?"):
        infohash = magnet_infohash(value)
        kind = "magnet"
    else:
        value = _http_url(value)
        try:
            validate_public_url(value)
        except ValueError:
            raise SourceError("资源地址未通过公网安全校验") from None
        kind = "torrent" if kind == "torrent" or unquote(urlsplit(value).path).lower().endswith(".torrent") else "http"
        infohash = torrent_infohash(fetch_torrent(value)) if kind == "torrent" else ""
    fingerprint = "bt:" + infohash if infohash else "url:" + hashlib.sha256(value.encode()).hexdigest()
    return {"title": str(title or "资源")[:500], "url": value, "kind": kind,
            "fingerprint": fingerprint, "infohash": infohash, "source_url": _source_label(source_url)}


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("a", "link") and attrs.get("href"):
            self.links.append((attrs["href"], attrs.get("type", ""), "download" in attrs))
        elif tag in ("video", "source") and attrs.get("src"):
            self.links.append((attrs["src"], attrs.get("type", "video/"), True))


def resolve_resources(url: str, title: str = "", limit: int = 20) -> list[dict]:
    """Resolve one public page; no recursive crawl, scripts or inferred instructions."""
    limit = max(1, min(int(limit), 30))
    deadline = time.monotonic() + 25
    if str(url).lower().startswith("magnet:?"):
        return [make_candidate(url, title)]
    value = _http_url(url)
    path = unquote(urlsplit(value).path).lower()
    if path.endswith((".torrent",) + MEDIA_EXTENSIONS):
        return [make_candidate(value, title, value)]
    response = _fetch(value)
    source_url = getattr(response, "url", "") or value
    content_type = str(response.headers.get("Content-Type", "")).lower()
    disposition = str(response.headers.get("Content-Disposition", "")).lower()
    if "bittorrent" in content_type or ".torrent" in disposition:
        infohash = torrent_infohash(response.content)
        return [{"title": str(title or "种子资源")[:500], "url": source_url, "kind": "torrent",
                 "infohash": infohash, "fingerprint": "bt:" + infohash, "source_url": _source_label(value)}]
    if content_type.startswith("video/"):
        return [make_candidate(source_url, title, value)]
    parser = _Links()
    parser.feed(response.text[:MAX_DOCUMENT])
    # Magnet strings may be plain text rather than anchors.
    links = parser.links + [(m, "", False) for m in re.findall(r'magnet:\?[^\s<>"\']+', html.unescape(response.text), re.I)]
    candidates = []
    seen = set()
    for target, mime, explicit_download in links[:300]:
        if time.monotonic() >= deadline:
            break
        target = urljoin(source_url, html.unescape(target))
        target_path = unquote(urlsplit(target).path).lower()
        is_torrent = "bittorrent" in mime or target_path.endswith(".torrent")
        if not (target.lower().startswith("magnet:?") or is_torrent or explicit_download
                or target_path.endswith(MEDIA_EXTENSIONS)):
            continue
        try:
            candidate = make_candidate(target, title, value, "torrent" if is_torrent else "")
        except (SourceError, ValueError):
            continue
        if candidate["fingerprint"] not in seen:
            seen.add(candidate["fingerprint"])
            candidates.append(candidate)
            if len(candidates) >= limit:
                break
    return candidates


def build_search_queries(requirement: dict) -> list[str]:
    if not isinstance(requirement, dict):
        raise SourceError("搜索条件须为对象")
    title = str(requirement.get("title") or requirement.get("query") or "").strip()
    if not title or len(title) > 300:
        raise SourceError("请输入不超过 300 字的番剧名称")
    aliases = requirement.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [a.strip() for a in re.split(r"[,，/]", aliases) if a.strip()]
    if not isinstance(aliases, list):
        raise SourceError("别名须为列表")
    terms = []
    for key, label in (("season", "季"), ("episode", "集"), ("quality", ""), ("subtitle", "")):
        if requirement.get(key) not in (None, ""):
            terms.append(str(requirement[key])[:80] + label)
    suffix = " ".join(terms + ["torrent magnet 下载"])
    return [f"{name} {suffix}" for name in dict.fromkeys([title] + [str(a)[:300] for a in aliases[:2]])]


def _rss_candidates(source: dict, requirement: dict, limit: int) -> list[dict]:
    deadline = time.monotonic() + 25
    url = str(source.get("url") or "")
    response = _fetch(url)
    raw = response.content
    if b"<!doctype" in raw.lower() or b"<!entity" in raw.lower():
        raise SourceError("RSS 不允许文档实体")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise SourceError("RSS 格式无效") from None
    aliases = requirement.get("aliases") or []
    if isinstance(aliases, str):
        aliases = re.split(r"[,，/]", aliases)
    terms = [str(requirement.get("title") or requirement.get("query") or "").casefold()]
    terms.extend(str(a).casefold().strip() for a in aliases[:10])
    results = []
    for entry in list(root.iter())[:5000]:
        if time.monotonic() >= deadline:
            break
        if entry.tag.split("}")[-1] not in ("item", "entry"):
            continue
        parts = list(entry)
        title = next((str(p.text or "") for p in parts if p.tag.split("}")[-1] == "title"), "")
        if not any(term and term in title.casefold() for term in terms):
            continue
        for node in parts:
            if time.monotonic() >= deadline:
                break
            name = node.tag.split("}")[-1]
            if name == "enclosure" or (name == "link" and node.get("rel") == "enclosure"):
                target = node.get("url") or node.get("href") or ""
                kind = "torrent" if "bittorrent" in node.get("type", "") else ""
                try:
                    results.append(make_candidate(urljoin(url, target), title, url, kind))
                except SourceError:
                    pass
            elif name == "attr" and node.get("name") == "magneturl":
                try:
                    results.append(make_candidate(node.get("value") or "", title, url))
                except SourceError:
                    pass
            elif name == "link":
                target = node.get("href") or node.text or ""
                if target:
                    try:
                        results.extend(resolve_resources(urljoin(url, target), title, limit - len(results)))
                    except SourceError:
                        pass
            if len(results) >= limit:
                return results[:limit]
    return results


def search_resources(requirement: dict, sources: list[dict] | None = None,
                     exclude_fingerprints: list[str] | None = None, limit: int = 10) -> list[dict]:
    """Search configured RSS feeds and the user's existing web search provider."""
    queries = build_search_queries(requirement)
    deadline = time.monotonic() + 60
    limit = max(1, min(int(limit), 30))
    excluded = set(str(x) for x in (exclude_fingerprints or []))
    candidates = []
    seen = set(excluded)

    def add(items):
        for item in items:
            if item["fingerprint"] not in seen and item.get("infohash") not in excluded and item["url"] not in excluded:
                seen.add(item["fingerprint"])
                candidates.append(item)

    for source in (sources or [])[:10]:
        if time.monotonic() >= deadline:
            return candidates[:limit]
        if not isinstance(source, dict) or source.get("enabled") is False:
            continue
        try:
            if source.get("kind", "rss") == "rss":
                add(_rss_candidates(source, requirement, limit))
        except SourceError:
            continue
        if len(candidates) >= limit:
            return candidates[:limit]
    search_sources = [s for s in (sources or []) if isinstance(s, dict) and s.get("kind") == "search"]
    if search_sources and not any(s.get("enabled") is not False for s in search_sources):
        return candidates[:limit]
    for query in queries:
        if time.monotonic() >= deadline:
            break
        try:
            hits = web_search_service.search(query, limit=min(limit, 10))
        except web_search_service.SearchError:
            continue
        for hit in hits[:10]:
            if time.monotonic() >= deadline:
                return candidates[:limit]
            try:
                add(resolve_resources(str(hit.get("url") or ""), str(hit.get("title") or ""), limit))
            except (SourceError, ValueError):
                continue
            if len(candidates) >= limit:
                return candidates[:limit]
    return candidates[:limit]
