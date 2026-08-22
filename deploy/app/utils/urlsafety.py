"""URL 公网校验：SSRF 防护（拒绝私网/回环/链路本地/云元数据地址）。

- validate_public_url：IP 字面量直接校验；域名则 DNS 解析后校验所有结果
- 配合 requests 的 response hook 使用，可在跟随重定向时逐跳校验
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_SCHEMES = ("http", "https")


class UrlSafetyError(ValueError):
    """URL 不是公网地址（SSRF 拦截）。"""


def _is_public_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def _host_is_public(host: str) -> bool:
    """校验主机名是否公网：IP 字面量直接判；域名解析后所有结果都需为公网。"""
    # IP 字面量（含 IPv6）
    if _is_public_ip(host):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # 不是 IP 字面量，按域名处理
    else:
        return False  # 是 IP 字面量但不是公网
    # 域名：DNS 解析，所有结果都必须是公网 IP
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise UrlSafetyError(f"域名解析失败：{host}") from None
    addrs = {info[4][0] for info in infos}
    if not addrs:
        raise UrlSafetyError(f"域名无解析结果：{host}")
    if not all(_is_public_ip(a) for a in addrs):
        bad = [a for a in addrs if not _is_public_ip(a)]
        raise UrlSafetyError(f"域名 {host} 解析到非公网地址：{bad[0]}")
    return True


def validate_public_url(url: str) -> str:
    """校验 URL 指向公网，返回规范化 URL；非法/非公网抛 UrlSafetyError。"""
    url = (url or "").strip()
    u = urlparse(url)
    if u.scheme not in _SCHEMES:
        raise UrlSafetyError("仅支持 http/https 地址")
    host = u.hostname
    if not host:
        raise UrlSafetyError("URL 缺少主机名")
    host = host.rstrip(".")
    try:
        public = _host_is_public(host)
    except UrlSafetyError:
        raise
    except Exception as e:  # noqa: BLE001
        raise UrlSafetyError(f"URL 校验失败：{e}") from e
    if not public:
        raise UrlSafetyError(f"拒绝访问非公网地址：{host}")
    return url


def requests_public_hook(resp, *args, **kwargs):
    """requests response hook：跟随重定向时逐跳校验最终/中间 URL 均为公网。"""
    validate_public_url(resp.url)
    return resp
