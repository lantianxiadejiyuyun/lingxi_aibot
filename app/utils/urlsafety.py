"""URL 公网校验与安全抓取：SSRF 防护（拒绝私网/回环/链路本地/云元数据等非公网地址）。

设计（2026 安全加固）：
- 复合负向 IP 判定：对 IPv6 先尝试 ipv4_mapped / sixtofour / teredo（取客户端 IPv4）
  归一化为 IPv4 再判；IPv4-compatible（::/96，如 ::127.0.0.1）整体拒绝；最终判定 =
  is_global 且不命中 is_private / is_loopback / is_link_local / is_reserved /
  is_multicast / is_unspecified / is_site_local。
- 消除 DNS rebinding TOCTOU：域名只在校验阶段解析一次且全部结果校验为公网，连接阶段
  不再解析域名——自定义 HTTPAdapter + urllib3 连接子类把 TCP 连接钉死在已验证的 IP，
  Host 头与 TLS SNI（server_hostname）仍使用原域名（_new_conn 内临时替换 _dns_host，
  建立连接后立即恢复）。逐跳重定向每跳重新校验并重新钉 IP。
- 响应大小上限：safe_get 内部始终流式读取，超过 max_response_bytes 直接拒绝。

对外接口：
- validate_public_url：IP 字面量直接校验；域名则 DNS 解析后校验所有结果
- safe_get：requests.get 的安全封装——allow_redirects=False 手动逐跳跟随，
  每一跳发起请求前先校验目标为公网并钉死已验证 IP，杜绝「重定向到内网的请求已发出」
  的 blind SSRF 与「校验后域名被改绑内网」的 DNS rebinding
- requests_public_hook：兼容旧调用点的 response hook（仅丢弃已响应内容，安全性弱于 safe_get）
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

_SCHEMES = ("http", "https")
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10MB
_MAX_REDIRECTS = 5
# IPv4-compatible IPv6（RFC 4291 已废弃）：低 32 位可伪装成任意 IPv4（如 ::127.0.0.1），整体拒绝
_IPV4_COMPAT_NET = ipaddress.ip_network("::/96")
# 强制直连（屏蔽系统/环境代理）：经代理无法保证连接目标就是已验证 IP
_NO_PROXY = {"http": None, "https": None}


class UrlSafetyError(ValueError):
    """URL 不是公网地址（SSRF 拦截）。"""


# ---------- IP 判定 ----------


def _parse_ip(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _embedded_ipv4(addr):
    """提取 IPv6 中内嵌的 IPv4（ipv4_mapped / sixtofour / teredo），无则返回 None。"""
    if addr.version != 6:
        return None
    if addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    if addr.sixtofour is not None:
        return addr.sixtofour
    if addr.teredo is not None:
        # teredo 属性返回 (server, client) 二元组，client 才是真实目标
        return addr.teredo[1]
    return None


def _addr_is_public(addr) -> bool:
    """复合负向判定：地址是否可安全作为公网连接目标。

    IPv6 先归一化为内嵌 IPv4 再判；IPv4-compatible（::/96）整体拒绝；
    最终 = is_global 且不命中任何负向条件（is_private/is_loopback/is_link_local/
    is_reserved/is_multicast/is_unspecified/is_site_local）。
    """
    if addr.version == 6:
        if addr in _IPV4_COMPAT_NET:
            return False
        embedded = _embedded_ipv4(addr)
        if embedded is not None:
            return _addr_is_public(embedded)
    return addr.is_global and not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or getattr(addr, "is_site_local", False)
    )


def _is_public_ip(ip: str) -> bool:
    """判断 IP 字符串是否公网（无法解析的字符串返回 False）。"""
    addr = _parse_ip(ip)
    if addr is None:
        return False
    return _addr_is_public(addr)


def _resolve_public_ips(host: str) -> list[str]:
    """解析主机名并返回全部公网 IP（IP 字面量直接判定）。

    任何结果含非公网地址、解析失败、无结果均抛 UrlSafetyError。
    这是安全抓取流程中对该域名唯一的一次 DNS 解析。
    """
    addr = _parse_ip(host)
    if addr is not None:
        if not _addr_is_public(addr):
            raise UrlSafetyError(f"拒绝访问非公网地址：{host}")
        return [str(addr)]
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise UrlSafetyError(f"域名解析失败：{host}") from None
    addrs: list[str] = []
    for info in infos:
        ip = info[4][0]
        addr = _parse_ip(ip)
        if addr is None or not _addr_is_public(addr):
            raise UrlSafetyError(f"域名 {host} 解析到非公网地址：{ip}")
        addrs.append(str(addr))
    if not addrs:
        raise UrlSafetyError(f"域名无解析结果：{host}")
    # 去重且保持顺序（多个 A/AAAA 记录时优先尝试前面的）
    seen: set[str] = set()
    uniq: list[str] = []
    for a in addrs:
        if a not in seen:
            seen.add(a)
            uniq.append(a)
    return uniq


def _host_is_public(host: str) -> bool:
    """兼容旧内部接口：主机名是否公网（布尔）。"""
    try:
        _resolve_public_ips(host)
        return True
    except UrlSafetyError:
        return False


# ---------- URL 校验 ----------


def _check_url_shape(url: str) -> str:
    """校验 URL 的 scheme/主机名存在性并返回规范化 URL（不做任何网络/DNS 操作）。"""
    url = (url or "").strip()
    u = urlparse(url)
    if u.scheme not in _SCHEMES:
        raise UrlSafetyError("仅支持 http/https 地址")
    host = u.hostname
    if not host:
        raise UrlSafetyError("URL 缺少主机名")
    return url


def validate_public_url(url: str) -> str:
    """校验 URL 指向公网，返回规范化 URL；非法/非公网抛 UrlSafetyError。"""
    url = _check_url_shape(url)
    host = (urlparse(url).hostname or "").rstrip(".")
    try:
        _resolve_public_ips(host)
    except UrlSafetyError:
        raise
    except Exception as e:  # noqa: BLE001
        raise UrlSafetyError(f"URL 校验失败：{e}") from e
    return url


def _addr_is_allowed_provider(addr, allow_private: bool) -> bool:
    """provider base_url 允许的目标：公网；allow_private 时另放开回环/私网（不含链路本地）。"""
    if _addr_is_public(addr):
        return True
    if not allow_private:
        return False
    return bool(addr.is_loopback or addr.is_private)


def _resolve_provider_ips(host: str, allow_private: bool) -> list[str]:
    """解析 provider 主机名：全部结果须为允许的地址。"""
    addr = _parse_ip(host)
    if addr is not None:
        if not _addr_is_allowed_provider(addr, allow_private):
            raise UrlSafetyError(f"拒绝访问非公网地址：{host}")
        return [str(addr)]
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise UrlSafetyError(f"域名解析失败：{host}") from None
    addrs: list[str] = []
    for info in infos:
        ip = info[4][0]
        addr = _parse_ip(ip)
        if addr is None or not _addr_is_allowed_provider(addr, allow_private):
            raise UrlSafetyError(f"域名 {host} 解析到不允许的地址：{ip}")
        addrs.append(str(addr))
    if not addrs:
        raise UrlSafetyError(f"域名无解析结果：{host}")
    seen: set[str] = set()
    uniq: list[str] = []
    for a in addrs:
        if a not in seen:
            seen.add(a)
            uniq.append(a)
    return uniq


def validate_provider_url(url: str, allow_private: bool = False) -> str:
    """校验 LLM/图片/TTS 等 provider base_url。

    默认必须公网；allow_private=True 时允许本机/局域网（Ollama 等），仍拒绝
    链路本地/元数据地址（如 169.254.169.254）。
    """
    url = _check_url_shape(url)
    host = (urlparse(url).hostname or "").rstrip(".")
    try:
        _resolve_provider_ips(host, allow_private)
    except UrlSafetyError:
        raise
    except Exception as e:  # noqa: BLE001
        raise UrlSafetyError(f"URL 校验失败：{e}") from e
    return url


def local_providers_allowed() -> bool:
    """是否允许本机/局域网 provider（ALLOW_LOCAL_PROVIDERS / 全局设置）。"""
    allow = False
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            allow = bool(current_app.config.get("ALLOW_LOCAL_PROVIDERS"))
            try:
                from app.services.settings_service import get_setting

                v = get_setting("allow_local_providers", None, user_id=0)
                if v is not None and str(v).strip() != "":
                    allow = str(v).strip().lower() not in ("0", "false", "no", "off")
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return allow


def check_provider_url(url: str) -> str:
    """校验 provider base_url（按是否允许本地 provider 决定私网）。非法抛 UrlSafetyError。"""
    return validate_provider_url(url, allow_private=local_providers_allowed())


def requests_public_hook(resp, *args, **kwargs):
    """requests response hook：跟随重定向时逐跳校验最终/中间 URL 均为公网。

    注意：requests 的重定向是「先请求后校验」，hook 只能丢弃响应、拦不住请求本身
    （blind SSRF）。新代码请优先使用 safe_get。
    """
    validate_public_url(resp.url)
    return resp


# ---------- 钉死已验证 IP 的连接机制 ----------


def _make_pinned_connection(base_cls, pinned_ip: str):
    """返回把 TCP 连接钉死到 pinned_ip 的 urllib3 连接子类。

    _new_conn 只在建立 socket 的瞬间把 _dns_host 换成已验证 IP，连接建立后立即恢复；
    因此 Host 头与 TLS SNI 仍使用原域名（Host 头在 putrequest 时才读取 host，
    HTTPS 的 server_hostname 在 connect() 里 _new_conn 返回之后才读取）。
    """

    class _PinnedConnection(base_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pinned_ip = pinned_ip

        def _new_conn(self):
            original = self._dns_host
            self._dns_host = pinned_ip
            try:
                return super()._new_conn()
            finally:
                self._dns_host = original

    return _PinnedConnection


def _make_pinned_pool_manager(pinned_ip: str):
    """返回连接池内所有连接都钉死到 pinned_ip 的 urllib3 PoolManager 子类。"""
    import urllib3.connection
    import urllib3.poolmanager

    class _PinnedPoolManager(urllib3.poolmanager.PoolManager):
        def __init__(self, *args, **kwargs):
            self._pinned_ip = pinned_ip
            super().__init__(*args, **kwargs)

        def _new_pool(self, scheme, host, port, request_context=None):
            pool = super()._new_pool(scheme, host, port, request_context)
            if scheme == "https":
                pool.ConnectionCls = _make_pinned_connection(
                    urllib3.connection.HTTPSConnection, self._pinned_ip
                )
            else:
                pool.ConnectionCls = _make_pinned_connection(
                    urllib3.connection.HTTPConnection, self._pinned_ip
                )
            return pool

    return _PinnedPoolManager


def _make_pinned_adapter(pinned_ip: str):
    """返回把连接钉死到已验证 IP 的 requests HTTPAdapter。"""
    import requests.adapters

    PinnedPoolManager = _make_pinned_pool_manager(pinned_ip)

    class _PinnedIPAdapter(requests.adapters.HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
            self.poolmanager = PinnedPoolManager(
                num_pools=connections, maxsize=maxsize, block=block, **pool_kwargs
            )

    return _PinnedIPAdapter()


# ---------- 安全抓取 ----------


def _cap_response(resp, max_response_bytes: int):
    """流式读取响应体并实施大小上限；超限抛 UrlSafetyError；读完后写回 resp._content。"""
    declared = None
    cl = resp.headers.get("Content-Length")
    if cl:
        try:
            declared = int(cl)
        except (TypeError, ValueError):
            declared = None
    if declared is not None and declared > max_response_bytes:
        raise UrlSafetyError(
            f"响应体超过大小上限：Content-Length={declared} > {max_response_bytes}"
        )
    # 多读 1 字节用于探测溢出；decode_content=True 按 Content-Encoding 解压后计数
    data = resp.raw.read(max_response_bytes + 1, decode_content=True)
    if len(data) > max_response_bytes:
        raise UrlSafetyError(f"响应体超过大小上限：>{max_response_bytes} 字节")
    resp._content = data


def _fetch_pinned(cur: str, ips: list[str], timeout, max_response_bytes: int, kwargs: dict):
    """用已验证的 IP 列表发起单次请求（不含重定向跟随）。

    每个 IP 尝试独立 session + 钉 IP 适配器，连接失败换下一个已验证 IP；
    响应体流式读取并实施大小上限。
    """
    import requests

    last_exc = None
    for ip in ips:
        session = requests.Session()
        session.mount("http://", _make_pinned_adapter(ip))
        session.mount("https://", _make_pinned_adapter(ip))
        resp = None
        try:
            resp = session.get(
                cur,
                timeout=timeout,
                allow_redirects=False,
                stream=True,
                proxies=_NO_PROXY,
                **kwargs,
            )
        except requests.ConnectionError as e:
            last_exc = e
            continue
        finally:
            if resp is None:
                session.close()
        try:
            _cap_response(resp, max_response_bytes)
        except Exception:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
            session.close()
            raise
        session.close()
        return resp
    if last_exc is not None:
        raise last_exc
    raise UrlSafetyError(f"无法连接：{cur}")


def safe_get(
    url: str,
    timeout: int = 10,
    max_redirects: int = _MAX_REDIRECTS,
    max_response_bytes: int = _MAX_RESPONSE_BYTES,
    **kwargs,
):
    """requests.get 的 SSRF 安全封装：逐跳校验 + 钉死已验证 IP，杜绝 DNS rebinding。

    - 每跳只解析一次 DNS：解析后全部结果须为公网，随后连接直接使用该 IP（不再解析域名）
    - 重定向逐跳重复校验；拒绝重定向到非公网（请求完全不会发出）
    - 响应体流式读取，超过 max_response_bytes 抛 UrlSafetyError
    - kwargs 透传（headers/verify 等）；不允许覆盖 allow_redirects/stream，且不支持代理
    """
    if kwargs.get("proxies"):
        raise UrlSafetyError("safe_get 不支持代理（无法保证连接目标为已验证 IP）")
    kwargs = dict(kwargs)
    kwargs.pop("allow_redirects", None)
    kwargs.pop("stream", None)  # 内部始终流式读取以实施大小上限
    kwargs.pop("proxies", None)
    max_response_bytes = max_response_bytes or _MAX_RESPONSE_BYTES
    resp = None
    try:
        cur = (url or "").strip()
        for _ in range(max_redirects + 1):
            cur = _check_url_shape(cur)
            host = (urlparse(cur).hostname or "").rstrip(".")
            ips = _resolve_public_ips(host)  # 本次跳的唯一一次 DNS 解析
            resp = _fetch_pinned(cur, ips, timeout, max_response_bytes, kwargs)
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location")
                resp.close()
                resp = None
                if not loc:
                    raise UrlSafetyError("重定向缺少 Location 头")
                cur = urljoin(cur, loc)
                continue
            return resp
        raise UrlSafetyError("重定向次数超限")
    except Exception:
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        raise
