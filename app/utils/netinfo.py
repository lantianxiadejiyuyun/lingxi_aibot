"""网页地址与本机选路诊断；不发送 HTTP 请求，不代表公网实测可达。"""
from __future__ import annotations

import ipaddress
import socket
import time
from typing import Any, Optional
from urllib.parse import urlsplit

_CACHE_TTL = 45.0
_cache: dict[str, Any] = {"at": 0.0, "data": None}

PAGE_UNREACHABLE_MSG = (
    "当前使用官方飞书 SDK 长连接：收消息不依赖入站端口，"
    "但 AI 构建的网页要在飞书里打开，仍需可从公网访问的地址。"
    "当前未检测到网页地址对应的公网 IPv4/IPv6；请在「网页站点」填写公网访问地址，"
    "并检查端口转发、防火墙和域名解析。此提示没有实测公网连通性。"
)


def _is_global_ipv4(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.version == 4 and addr.is_global


def _probe_local_ipv4() -> tuple[bool, Optional[str]]:
    """只查询 IPv4 选路（UDP connect 不发送数据），不验证公网连通性。"""
    for dest in (("223.5.5.5", 53), ("8.8.8.8", 80)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(2)
                sock.connect(dest)
                ip = sock.getsockname()[0]
            return True, ip
        except OSError:
            continue
    return False, None


def _host_has_public_a(host: str) -> bool:
    return _host_public_addresses(host)[0]


def _host_public_addresses(host: str) -> tuple[bool, bool]:
    """返回（仅含公网 IPv4、仅含公网 IP）；DNS 结果不等于服务可达。"""
    host = (host or "").strip().strip("[]")
    if not host or host.lower() == "localhost":
        return False, False
    try:
        address = ipaddress.ip_address(host)
        return address.version == 4 and address.is_global, address.is_global
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
        addresses = {ipaddress.ip_address(info[4][0]) for info in infos}
    except (OSError, ValueError):
        return False, False
    ipv4 = {address for address in addresses if address.version == 4}
    return (bool(ipv4) and all(address.is_global for address in ipv4),
            bool(addresses) and all(address.is_global for address in addresses))


def clear_page_reachability_cache() -> None:
    """网页地址设置保存后，立即丢弃旧诊断。"""
    _cache.update(at=0.0, data=None)


def diagnose_page_reachability() -> dict[str, Any]:
    """诊断地址是否对应公网 IP；缓存 45 秒，不进行 HTTP/端口连通测试。"""
    now = time.time()
    if _cache["data"] is not None and now - _cache["at"] < _CACHE_TTL:
        return dict(_cache["data"])

    from app.services.page_service import (
        page_access_host, page_port_configured, page_public_base_url_configured,
    )

    outbound, local_ip = _probe_local_ipv4()
    local_public = bool(local_ip and _is_global_ipv4(local_ip))
    public_base = page_public_base_url_configured()
    parsed_public = urlsplit(public_base) if public_base else None
    page_host = parsed_public.hostname if parsed_public else page_access_host(local_ip or "")
    host_public_ipv4, host_public = _host_public_addresses(page_host)
    port = page_port_configured()
    public_port = (parsed_public.port or (443 if parsed_public.scheme == "https" else 80)) \
        if parsed_public else port
    data = {
        "outbound_ipv4": outbound,
        "local_ipv4": local_ip or "",
        "local_ipv4_public": local_public,
        "page_host": page_host,
        "page_host_public_ipv4": host_public_ipv4,
        "page_host_public_ip": host_public,
        "page_port": port,
        "page_public_base_url": public_base,
        "page_public_port": public_port,
        # 兼容旧调用方：这里只表示地址具备公网 IP 条件，不能保证转发/防火墙放行。
        "pages_openable": host_public,
        "reachability_verified": False,
        "diagnostic_note": "仅检查地址与 DNS，未实测公网端口连通性；仍需确认端口转发和防火墙。",
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
