"""本机 IPv4 / 网页公网可达性探测（飞书 SDK 场景提示用）。"""
from __future__ import annotations

import ipaddress
import socket
import time
from typing import Any, Optional

_CACHE_TTL = 45.0
_cache: dict[str, Any] = {"at": 0.0, "data": None}

PAGE_UNREACHABLE_MSG = (
    "当前使用官方飞书 SDK 长连接：收消息不依赖入站端口，"
    "但 AI 构建的网页要在飞书里打开，必须有公网 IPv4 可达地址"
    "（设置「网页站点」端口，并把该端口映射到公网 IPv4，或给网页域名做 A 记录）。"
    "未连接公网、没有公网 IPv4 时，飞书里无法显示构建的网页。"
)


def _is_global_ipv4(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.version == 4 and addr.is_global


def _probe_local_ipv4() -> tuple[bool, Optional[str]]:
    """探测是否有 IPv4 出口；返回 (能否连公网, 本机出口地址)。"""
    for dest in (("223.5.5.5", 53), ("8.8.8.8", 80)):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(2)
            sock.connect(dest)
            ip = sock.getsockname()[0]
            sock.close()
            return True, ip
        except OSError:
            continue
    return False, None


def _host_has_public_a(host: str) -> bool:
    host = (host or "").strip().split("/")[0].split(":")[0]
    if not host or host in ("localhost", "127.0.0.1"):
        return False
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    addrs = {info[4][0] for info in infos}
    return bool(addrs) and all(_is_global_ipv4(a) for a in addrs)


def diagnose_page_reachability() -> dict[str, Any]:
    """诊断：本机 IPv4 出口 + 网页域名是否解析到公网 IPv4。结果缓存约 45 秒。"""
    now = time.time()
    if _cache["data"] is not None and now - _cache["at"] < _CACHE_TTL:
        return dict(_cache["data"])

    from app.services.page_service import page_access_host, page_port_configured

    outbound, local_ip = _probe_local_ipv4()
    local_public = bool(local_ip and _is_global_ipv4(local_ip))
    page_host = page_access_host()
    host_public = _host_has_public_a(page_host) if page_host else False
    port = page_port_configured()
    # 独立 HTTP 端口 + 本机有公网 IPv4 时，飞书也能打开 http://公网IP:端口/webs/html/slug
    pages_ok = host_public or bool(port and local_public)
    data = {
        "outbound_ipv4": outbound,
        "local_ipv4": local_ip or "",
        "local_ipv4_public": local_public,
        "page_host": page_host,
        "page_host_public_ipv4": host_public,
        "page_port": port,
        "pages_openable": pages_ok,
    }
    _cache["at"] = now
    _cache["data"] = data
    return dict(data)


def feishu_sdk_page_warning() -> str:
    """SDK 长连接且网页对飞书不可达时返回提示文案，否则空串。"""
    try:
        from app.services.feishu_ws import MODE_SDK, receive_mode

        if receive_mode() != MODE_SDK:
            return ""
    except Exception:  # noqa: BLE001
        return ""
    diag = diagnose_page_reachability()
    if diag["pages_openable"]:
        return ""
    return PAGE_UNREACHABLE_MSG


def reply_looks_like_page(text: str) -> bool:
    t = text or ""
    if "访问地址" in t or "/p/" in t or "/webs/html/" in t:
        return True
    return False
