"""Account-owned media settings. Downloader credentials never enter AI context."""
import base64
import hashlib
import json
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

from app.models.user import User
from app.extensions import db
from app.services.settings_service import get_own_setting, set_setting


class MediaError(ValueError):
    def __init__(self, message, code="invalid_media_request", status=400):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


def parse_id(value, name="ID", minimum=1):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise MediaError(f"{name} 必须为整数")
    if isinstance(value, str) and (not value.isascii() or not value.isdigit() or len(value) > 19):
        raise MediaError(f"{name} 必须为整数")
    value = int(value)
    if not minimum <= value <= 2**63 - 1:
        raise MediaError(f"{name} 超出范围")
    return value


def _cipher(user_id):
    key = hashlib.sha256((str(current_app.config["SECRET_KEY"]) + f":media:{user_id}").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def require_admin(user_id):
    user = db.session.get(User, user_id)
    if not user or not user.is_admin:
        raise MediaError("只有管理员可以登记存储根目录和下载器", "forbidden", 403)


def read_config(user_id, *, secrets=False):
    raw = get_own_setting("media_config", {}, user_id=user_id)
    sources = raw.get("sources", [])
    if raw.get("sources_ciphertext"):
        try:
            sources = json.loads(_cipher(user_id).decrypt(raw["sources_ciphertext"].encode()))
        except (InvalidToken, ValueError):
            raise MediaError("搜索源凭据无法解密，请重新保存", "credentials_unavailable", 409) from None
    config = {"downloaders": [], "sources": sources if secrets else [
        {"name": s.get("name", ""), "kind": s.get("kind"), "enabled": s.get("enabled", True), "configured": True} for s in sources]}
    for item in raw.get("downloaders", []):
        value = {k: v for k, v in item.items() if k != "credentials"}
        value["configured"] = True
        if secrets and item.get("credentials"):
            try:
                value.update(json.loads(_cipher(user_id).decrypt(item["credentials"].encode())))
            except (InvalidToken, ValueError):
                raise MediaError("下载器凭据无法解密，请重新保存配置", "credentials_unavailable", 409) from None
        config["downloaders"].append(value)
    return config


def save_config(user_id, data):
    require_admin(user_id)
    actor_id = user_id
    if data.get("owner_id") not in (None, ""):
        user_id = parse_id(data["owner_id"], "用户 ID")
        if db.session.get(User, user_id) is None:
            raise MediaError("目标用户不存在", "not_found", 404)
    old = get_own_setting("media_config", {}, user_id=user_id)
    rows = data.get("downloaders", old.get("downloaders", []))
    if actor_id != user_id and isinstance(rows, list) and all(isinstance(r, dict) for r in rows):
        submitted = {r.get("kind") for r in rows}
        rows = [r for r in old.get("downloaders", []) if r["kind"] not in submitted] + rows
    if not isinstance(rows, list) or len(rows) > 2:
        raise MediaError("最多配置一个 qBittorrent 和一个 aria2")
    downloaders, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise MediaError("下载器配置格式无效")
        kind = row.get("kind")
        if kind not in ("qbittorrent", "aria2") or kind in seen:
            raise MediaError("下载器类型无效或重复")
        seen.add(kind)
        url = str(row.get("base_url", "")).strip().rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise MediaError("下载器地址须为不含账号、参数的 HTTP(S) 地址")
        previous = next((r for r in old.get("downloaders", []) if r["kind"] == kind), {})
        credentials = {}
        if previous.get("credentials"):
            try:
                credentials = json.loads(_cipher(user_id).decrypt(previous["credentials"].encode()))
            except (InvalidToken, ValueError):
                pass
        for key in ("username", "password", "secret"):
            if key in row and row[key] != "":
                if not isinstance(row[key], str) or len(row[key]) > 4096:
                    raise MediaError("下载器凭据格式无效")
                credentials[key] = row[key]
        downloaders.append({"kind": kind, "base_url": url, "timeout": 15,
                            "credentials": _cipher(user_id).encrypt(json.dumps(credentials).encode()).decode()})
    sources = data["sources"] if "sources" in data else read_config(user_id, secrets=True)["sources"]
    if not isinstance(sources, list) or len(sources) > 20 or any(not isinstance(s, dict) for s in sources):
        raise MediaError("搜索源须为最多 20 项的列表")
    from app.services.media_sources import validate_source_config
    sources = [validate_source_config(s) for s in sources]
    set_setting("media_config", {"downloaders": downloaders, "sources_ciphertext": _cipher(user_id).encrypt(json.dumps(sources).encode()).decode()}, user_id=user_id)
    return read_config(user_id)


def worker_status():
    from datetime import datetime, timedelta
    from app.services.settings_service import get_setting
    from app.utils.timeutil import utcnow
    last = get_setting("media_worker_heartbeat", None, user_id=0)
    try:
        active = datetime.fromisoformat(last) > utcnow() - timedelta(seconds=60)
    except (ValueError, TypeError):
        active = False
    return {"worker_active": active, "worker_last_seen": last}


def downloader_config(user_id, kind):
    cfg = next((d for d in read_config(user_id, secrets=True)["downloaders"] if d["kind"] == kind), None)
    if not cfg:
        raise MediaError(f"请先配置 {kind} 下载器", "downloader_unconfigured", 409)
    return cfg
