"""Read an authorized navigation vault without caching or persisting secrets.

List operations expose metadata only. Revealing one item is a separate explicit
operation intended for the protected UI/API, never for an AI tool or history.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import hmac
import ipaddress
import json
import re
from urllib.parse import urlsplit

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extensions import db
from app.models.setting import Setting
from app.services.navigation_vault_service import (
    SETTING_KEY, VaultRegistrationError, decrypt_registration, validate_read_url,
)
from app.utils.integration_api import ApiError
from app.utils.scoping import current_user_id
from app.utils.urlsafety import (
    UrlSafetyError, _cap_response, _make_pinned_adapter, _resolve_public_ips,
)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 10000
MAX_SAFE_INTEGER = 2 ** 53 - 1
_INVALID_RESPONSE = "导航密码服务返回的数据格式无效或超过限制"


def _authorize_user(user_id):
    if type(user_id) is not int or user_id < 1 or current_user_id() != user_id:
        raise ApiError("无权读取此账号的导航凭据", 403, "forbidden")


def _fresh_registration(user_id):
    # A separate short transaction sees revocation under MySQL REPEATABLE READ,
    # without detaching or committing the caller's chat/request ORM objects.
    with Session(db.engine) as session:
        record = session.scalar(select(Setting.value).where(
            Setting.key == SETTING_KEY, Setting.user_id == user_id,
        ))
        record = deepcopy(record)
    try:
        registration = decrypt_registration(user_id, record=record)
    except VaultRegistrationError:
        raise ApiError("导航密码授权未配置、已过期或已撤销，请重新绑定", 403, "vault_unavailable") from None
    fingerprint = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=True,
                                            separators=(",", ":")).encode("utf-8")).digest()
    return registration, fingerprint


def _post_read(registration, body):
    """One HTTPS POST through a DNS-pinned connection; no redirects/proxies."""
    try:
        url = validate_read_url(registration["read_url"], check_dns=False)
        host = urlsplit(url).hostname
        ips = _resolve_public_ips(host)
        with requests.Session() as session:
            session.trust_env = False
            session.mount("https://", _make_pinned_adapter(ips[0]))
            response = session.post(url, json=body,
                headers={"Authorization": "Bearer " + registration["read_token"],
                         "Accept": "application/json"},
                timeout=(5, 15), allow_redirects=False, stream=True)
            try:
                status = response.status_code
                if status in (401, 403):
                    raise ApiError("导航密码读取授权已失效，请重新授权", 403, "vault_unavailable")
                if status == 409:
                    raise ApiError("导航密码副本待更新，请在插件中同步", 409, "vault_stale")
                if status == 503:
                    raise ApiError("导航密码副本无法解密，请在插件中重新授权同步", 503,
                                   "vault_decryption_failed")
                if status != 200:
                    raise ApiError("导航密码服务暂时不可用", 502, "vault_upstream_error")
                _cap_response(response, MAX_RESPONSE_BYTES)
                raw = response.content
            finally:
                response.close()
        # Never pass upstream error bodies, exception strings or raw content to
        # logs/responses. JSON must be valid UTF-8; field validation follows.
        def invalid_constant(_value):
            raise ValueError()
        return json.loads(raw.decode("utf-8"), parse_constant=invalid_constant)
    except ApiError:
        raise
    except UrlSafetyError:
        raise ApiError("导航密码服务地址或响应不符合访问限制", 502, "vault_upstream_error") from None
    except (ValueError, UnicodeError, RecursionError):
        raise ApiError(_INVALID_RESPONSE, 502, "invalid_response") from None
    except Exception:
        # requests exceptions may contain authorization headers or URLs.
        raise ApiError("无法连接导航密码服务，请稍后重试", 502, "vault_upstream_error") from None


def _text(value, max_chars=100000, *, nonempty=False):
    if not isinstance(value, str) or len(value) > max_chars or (nonempty and not value):
        raise ValueError()
    if len(value.encode("utf-8")) > max_chars * 4:
        raise ValueError()
    return value


def _item_id(value):
    value = _text(value, 200, nonempty=True)
    if not value.strip() or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError()
    return value


def _validate_snapshot(payload):
    try:
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list) \
                or len(payload["items"]) > MAX_ITEMS:
            raise ValueError()
        for name in ("snapshot_version", "source_version", "updated_at"):
            if type(payload.get(name)) is not int or not 0 <= payload[name] <= MAX_SAFE_INTEGER:
                raise ValueError()
        seen = set()
        for item in payload["items"]:
            if not isinstance(item, dict):
                raise ValueError()
            item_id = _item_id(item.get("id"))
            if item_id in seen:
                raise ValueError()
            seen.add(item_id)
            for name in ("title", "url", "username", "password", "notes"):
                _text(item.get(name))
            for name in ("createdAt", "updatedAt"):
                if name in item and (type(item[name]) is not int or not 0 <= item[name] <= MAX_SAFE_INTEGER):
                    raise ValueError()
        return payload["items"]
    except (ValueError, UnicodeError, TypeError):
        raise ApiError(_INVALID_RESPONSE, 502, "invalid_response") from None


def _read_items(user_id, body, grant_hash=None):
    _authorize_user(user_id)
    registration, fingerprint = _fresh_registration(user_id)
    if grant_hash is not None:
        if not isinstance(grant_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", grant_hash):
            raise ApiError("导航密码来源已变化，请在当前导航站重新绑定授权", 409, "vault_source_changed")
        current_hash = hashlib.sha256(registration["read_token"].encode("utf-8")).hexdigest()
        if not hmac.compare_digest(current_hash, grant_hash.lower()):
            raise ApiError("导航密码来源已变化，请在当前导航站重新绑定授权", 409, "vault_source_changed")
    payload = _post_read(registration, body)
    items = _validate_snapshot(payload)
    # Re-read after the callback, not from the request's identity map/transaction.
    # Discard its data if a DELETE, renewal, account change or expiry raced it.
    _current, latest = _fresh_registration(user_id)
    if not hmac.compare_digest(fingerprint, latest):
        raise ApiError("导航密码授权已变化，请刷新后重试", 409, "vault_grant_changed")
    return items


def _site_origin(value):
    try:
        if any(ord(character) <= 32 for character in value) or "\\" in value:
            return ""
        url = urlsplit(value)
        if url.scheme not in ("http", "https") or not url.hostname:
            return ""
        hostname = url.hostname.lower()
        try:
            address = ipaddress.ip_address(hostname)
            host = f"[{address}]" if address.version == 6 else str(address)
        except ValueError:
            host = hostname.encode("idna").decode("ascii")
            if not re.fullmatch(r"[a-z0-9.-]+", host) or len(host) > 253:
                return ""
        port = url.port
        if port == 0:
            return ""
        suffix = f":{port}" if port and port != {"http": 80, "https": 443}[url.scheme] else ""
        return f"{url.scheme}://{host}{suffix}"
    except (ValueError, UnicodeError):
        return ""


def _masked_username(value):
    if not value:
        return ""
    if len(value) < 3 or any(ord(character) < 32 for character in (value[0], value[-1])):
        return "***"
    return value[0] + "***" + value[-1]


def list_vault_items(user_id, q="", limit=50, offset=0, *, grant_hash=None):
    _authorize_user(user_id)
    try:
        q = _text(q, 200).strip().casefold()
    except (ValueError, UnicodeError):
        raise ApiError("q 必须为不超过 200 字符的有效文本") from None
    if type(limit) is not int or not 1 <= limit <= 200 \
            or type(offset) is not int or not 0 <= offset <= 1000000:
        raise ApiError("分页参数无效")
    # The peer currently returns a snapshot, so plaintext exists briefly here;
    # only whitelisted metadata survives this function or reaches AI callers.
    items = _read_items(user_id, {}, grant_hash=grant_hash)
    metadata = [{"id": item["id"], "title": item["title"], "site": _site_origin(item["url"]),
                 "username_masked": _masked_username(item["username"]),
                 "password_set": bool(item["password"])} for item in items]
    del items
    if q:
        metadata = [item for item in metadata if any(q in item[key].casefold() for key in ("id", "title", "site"))]
    total = len(metadata)
    return {"items": metadata[offset:offset + limit],
            "pagination": {"limit": limit, "offset": offset, "total": total}}


def reveal_vault_item(user_id, item_id, *, grant_hash=None):
    _authorize_user(user_id)
    try:
        item_id = _item_id(item_id)
    except (ValueError, UnicodeError):
        raise ApiError("id 必须为 1–200 字符的有效文本") from None
    items = _read_items(user_id, {"ids": [item_id]}, grant_hash=grant_hash)
    if not items:
        raise ApiError("导航密码条目不存在", 404, "not_found")
    if len(items) != 1 or items[0]["id"] != item_id:
        raise ApiError(_INVALID_RESPONSE, 502, "invalid_response")
    item = items[0]
    return {"id": item["id"], "title": item["title"], "site": _site_origin(item["url"]),
            "username": item["username"], "password": item["password"]}
