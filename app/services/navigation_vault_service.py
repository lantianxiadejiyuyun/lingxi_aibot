"""Register an encrypted, short-lived navigation callback capability.

This module never calls the callback or exposes credentials to AI tools. The
separate reader validates DNS afresh, pins the checked public address and disables
redirects for every request; registration-time validation cannot replace that.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta
import hashlib
import ipaddress
import json
import secrets
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import current_app

from app.extensions import db
from app.models.setting import Setting
from app.services.settings_service import get_own_setting, set_setting
from app.utils.integration_api import ApiError, iso_datetime, parse_datetime
from app.utils.timeutil import utcnow
from app.utils.urlsafety import UrlSafetyError, validate_public_url

SETTING_KEY = "integration:navigation_vault"
DEFAULT_ALLOWED_HOSTS = ("index.eugenstudio.cn", "ojjlab.eugenstudio.cn")
READ_PATH = "/api/lingxi/vault/service/read"
_FIELDS = {"read_url", "read_token", "expires_at", "scope", "navigation_user_id"}
_KEY_DOMAIN = b"lingxi:navigation-vault:aes-256-gcm:v1\x00"
_UNIX_EPOCH = datetime(1970, 1, 1)
_MAX_UNIX_MILLISECONDS = 253402300799999
_UNSET_RECORD = object()


class VaultRegistrationError(ValueError):
    """An absent, expired or modified registration must be bound again."""


def _user_id(value):
    if type(value) is not int or value < 1:
        raise ValueError("必须指定当前灵犀账号")
    return value


def _navigation_id(value):
    if type(value) is int:
        if value < 1:
            raise ApiError("navigation_user_id 必须为正整数或非空文本")
        value = str(value)
    if not isinstance(value, str) or not value.strip() or len(value) > 128 \
            or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ApiError("navigation_user_id 必须为 1–128 字符的文本或正整数")
    return value.strip()


def _read_token(value):
    if not isinstance(value, str) or not 16 <= len(value) <= 4096 \
            or any(not 33 <= ord(character) <= 126 for character in value):
        raise ApiError("read_token 必须为 16–4096 个可见 ASCII 字符，不能包含空白")
    return value


def _parse_expiry(value):
    if type(value) is int:
        if not 0 < value <= _MAX_UNIX_MILLISECONDS:
            raise ApiError("expires_at 必须为有效的正整数 Unix 毫秒时间戳")
        # Keep integer arithmetic: float timestamps can round milliseconds.
        seconds, milliseconds = divmod(value, 1000)
        return _UNIX_EPOCH + timedelta(seconds=seconds, microseconds=milliseconds * 1000)
    if isinstance(value, str):
        return parse_datetime(value, "expires_at")
    raise ApiError("expires_at 必须为正整数 Unix 毫秒时间戳或带时区的 RFC 3339 时间")


def _unix_milliseconds(value):
    delta = value - _UNIX_EPOCH
    return delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000


def validate_read_url(value, *, check_dns=True):
    if not isinstance(value, str) or len(value) > 2048 \
            or any(ord(character) <= 32 or ord(character) >= 127 for character in value) \
            or any(character in value for character in "\\?#"):
        raise ApiError("read_url 必须为允许的 HTTPS 回调地址")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if not host or parsed.scheme != "https" or parsed.port is not None \
                or parsed.username is not None or parsed.password is not None \
                or parsed.path != READ_PATH or parsed.query or parsed.fragment \
                or parsed.netloc.lower() != host:
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError()
        configured = current_app.config.get("NAVIGATION_VAULT_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS)
        if isinstance(configured, str):
            configured = configured.split(",")
        allowed = {str(item).strip().lower() for item in configured}
        if host not in allowed:
            raise ValueError()
    except (ValueError, TypeError):
        raise ApiError("read_url 仅允许配置白名单中的 HTTPS 导航站固定回调路径") from None
    normalized = f"https://{host}{READ_PATH}"
    if check_dns:
        try:
            validate_public_url(normalized)
        except UrlSafetyError:
            raise ApiError("read_url 域名必须解析为公网地址") from None
    return normalized


def _cipher():
    secret = current_app.config.get("SECRET_KEY")
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if not isinstance(secret, bytes) or not secret:
        raise ApiError("服务器未配置加密密钥", status=503, code="unavailable")
    return AESGCM(hashlib.sha256(_KEY_DOMAIN + secret).digest())


def _aad(user_id, metadata):
    value = {key: metadata[key] for key in ("read_url", "expires_at", "scope", "navigation_user_id")}
    value.update({"user_id": user_id, "purpose": "navigation-vault", "version": 1})
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def register_navigation_vault(user_id, payload):
    user_id = _user_id(user_id)
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ApiError("必须且只能提供 read_url、read_token、expires_at、scope、navigation_user_id")
    if payload["scope"] != "all":
        raise ApiError("scope 当前仅支持 all")
    navigation_user_id = _navigation_id(payload["navigation_user_id"])
    token = _read_token(payload["read_token"])
    expires_at = _parse_expiry(payload["expires_at"])
    now = utcnow()
    if not now < expires_at <= now + timedelta(hours=24):
        raise ApiError("expires_at 必须在未来 24 小时内")
    read_url = validate_read_url(payload["read_url"])
    metadata = {"read_url": read_url, "expires_at": iso_datetime(expires_at),
                "scope": "all", "navigation_user_id": navigation_user_id}
    nonce = secrets.token_bytes(12)
    encrypted = _cipher().encrypt(nonce, token.encode("ascii"), _aad(user_id, metadata))
    record = {**metadata, "version": 1,
              "token_ciphertext": base64.b64encode(nonce + encrypted).decode("ascii")}
    # All parsing, URL checks and encryption finish before changing the old row.
    set_setting(SETTING_KEY, record, user_id=user_id)
    return _status(metadata, configured=True)


def decrypt_registration(user_id, *, record=_UNSET_RECORD):
    """Return a verified registration to trusted server code; never make HTTP.

    The caller must already authorize this user. Plaintext must not be logged,
    returned by API routes, or placed in model prompts/history.
    """
    user_id = _user_id(user_id)
    if record is _UNSET_RECORD:
        record = get_own_setting(SETTING_KEY, user_id=user_id)
    try:
        if not isinstance(record, dict) or type(record.get("version")) is not int \
                or record.get("version") != 1 \
                or record.get("scope") != "all":
            raise ValueError()
        if _navigation_id(record.get("navigation_user_id")) != record["navigation_user_id"]:
            raise ValueError()
        if parse_datetime(record.get("expires_at"), "expires_at") <= utcnow():
            raise ValueError()
        if validate_read_url(record.get("read_url"), check_dns=False) != record["read_url"]:
            raise ValueError()
        encoded = record["token_ciphertext"]
        if not isinstance(encoded, str) or len(encoded) > 8192:
            raise ValueError()
        encrypted = base64.b64decode(encoded, validate=True)
        if len(encrypted) < 44:
            raise ValueError()
        token = _cipher().decrypt(encrypted[:12], encrypted[12:], _aad(user_id, record)).decode("ascii")
        _read_token(token)
        return {"read_url": record["read_url"], "read_token": token,
                "expires_at": record["expires_at"], "scope": record["scope"],
                "navigation_user_id": record["navigation_user_id"]}
    except (ValueError, KeyError, TypeError, InvalidTag):
        raise VaultRegistrationError("导航凭据登记不可用，请重新绑定") from None


def _status(metadata=None, *, configured=False):
    return {"configured": configured,
            "navigation_user_id": metadata["navigation_user_id"] if metadata else None,
            "expires_at": _unix_milliseconds(parse_datetime(metadata["expires_at"], "expires_at"))
            if metadata else None,
            "scope": metadata["scope"] if metadata else None}


def navigation_vault_status(user_id):
    try:
        registration = decrypt_registration(user_id)
    except VaultRegistrationError:
        return _status()
    return _status(registration, configured=True)


def unregister_navigation_vault(user_id):
    user_id = _user_id(user_id)
    Setting.query.filter_by(key=SETTING_KEY, user_id=user_id).delete(synchronize_session=False)
    db.session.commit()
    return _status()
