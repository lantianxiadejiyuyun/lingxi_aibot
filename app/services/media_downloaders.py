"""Small native qBittorrent Web API / aria2 JSON-RPC adapters.

Callers persist attempt_key BEFORE submit. A timeout is an uncertain submission,
not permission to create a fresh attempt: reconcile the same key on the next tick.
Only administrator-configured endpoint URLs may address the LAN. Untrusted source
URLs must be public. For untrusted HTTP downloads, aria2's network must additionally
block private/metadata destinations: its RPC API cannot pin or inspect redirects.
"""
from __future__ import annotations

import hashlib
import ipaddress
import posixpath
import re
from urllib.parse import unquote, urlsplit

import requests

from app.services.media_sources import (SourceError, _http_url, fetch_torrent,
                                        magnet_infohash, torrent_infohash)
from app.utils.urlsafety import (_make_pinned_adapter, _resolve_provider_ips,
                                 validate_public_url)


class DownloadError(RuntimeError):
    def __init__(self, message: str, *, code="download_error", infrastructure=False, uncertain=False):
        super().__init__(message)
        self.message = message
        self.code = code
        self.infrastructure = infrastructure
        self.uncertain = uncertain


def validate_downloader_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise DownloadError("下载器配置须为对象", code="config", infrastructure=True)
    result = dict(config)
    result["kind"] = str(result.get("kind") or "").lower()
    if result["kind"] not in ("aria2", "qbittorrent"):
        raise DownloadError("下载器类型应为 aria2 或 qbittorrent", code="config", infrastructure=True)
    try:
        url = _http_url(str(result.get("base_url") or ""))
        parts = urlsplit(url)
        if parts.query or parts.fragment:
            raise ValueError()
        result["base_url"] = url.rstrip("/")
        result["timeout"] = max(2, min(float(result.get("timeout", 15)), 60))
    except (ValueError, TypeError):
        raise DownloadError("下载器接口地址或超时配置无效", code="config", infrastructure=True) from None
    # Credentials are passed separately, never inside URLs or errors.
    for key in ("username", "password", "secret"):
        result[key] = str(result.get(key) or "")
    return result


def _number(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _empty(external_id, state="queued"):
    return {"external_id": external_id, "state": state, "downloaded_bytes": 0,
            "total_bytes": 0, "speed": 0, "files": [], "error": "",
            "infrastructure": False}


def _check_destination(destination):
    value = str(destination or "")
    # Destination comes only from the storage service's administrator-defined map.
    if not value or not (value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value)) or "\x00" in value:
        raise DownloadError("下载保存目录必须是已配置的绝对路径", code="destination", infrastructure=True)
    return value


class _Endpoint:
    def __init__(self, config):
        self.config = validate_downloader_config(config)
        self.base_url = self.config["base_url"]
        self.timeout = self.config["timeout"]
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"Referer": self.base_url, "User-Agent": "Lingxi-media/1"})
        # Resolve once and pin LAN endpoint as well; do not follow redirects that
        # could leak credentials to an unrelated origin.
        try:
            addresses = _resolve_provider_ips(urlsplit(self.base_url).hostname, allow_private=True)
            for address in addresses:
                parsed = ipaddress.ip_address(address)
                if parsed.is_link_local or parsed.is_multicast or parsed.is_unspecified or parsed.is_reserved:
                    raise ValueError()
            adapter = _make_pinned_adapter(addresses[0])
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)
        except (ValueError, IndexError):
            self.session.close()
            raise DownloadError("下载器地址解析失败或不允许访问", code="config", infrastructure=True) from None

    def close(self):
        self.session.close()

    def _request(self, method, url, *, mutation=False, **kwargs):
        try:
            response = self.session.request(method, url, timeout=self.timeout, allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise DownloadError("下载器暂时无法连接", code="unavailable", infrastructure=True,
                                uncertain=mutation) from None
        if response.status_code in (401, 403):
            raise DownloadError("下载器认证失败，请检查连接配置", code="authentication", infrastructure=True)
        if 300 <= response.status_code < 400:
            raise DownloadError("下载器接口重定向已拒绝，请填写最终接口地址", code="redirect", infrastructure=True)
        if response.status_code >= 500:
            raise DownloadError("下载器服务暂时不可用", code="unavailable", infrastructure=True,
                                uncertain=mutation)
        return response


class QBittorrentDownloader(_Endpoint):
    kind = "qbittorrent"

    def __init__(self, config):
        super().__init__(config)
        self._authenticated = False

    @staticmethod
    def external_id(attempt_key):
        # qB identifies downloads by infohash; deterministic tag locates it before
        # the database has recorded the returned hash.
        return "lingxi-" + hashlib.sha256(str(attempt_key).encode()).hexdigest()[:24]

    def _call(self, method, *, data=None, params=None, files=None, mutation=False, fallback=None):
        # qB hashes identify content, not the lifetime of a task. A user may have
        # removed our torrent and added that same hash again independently.
        # Revalidate the attempt tag before EVERY control mutation, including a
        # compatibility fallback, instead of trusting an old database hash.
        controls = {"torrents/start", "torrents/resume", "torrents/stop", "torrents/pause",
                    "torrents/reannounce", "torrents/delete"}
        if mutation and method in controls and self.config.get("attempt_key"):
            if self._owned_hash((data or {}).get("hashes", "")) is None:
                # An already removed torrent makes stop/delete idempotent. No
                # network mutation occurred; retry() subsequently polls missing.
                response = requests.Response()
                response.status_code = 200
                response._content = b""
                return response
        if not self._authenticated:
            response = self._request("POST", self.base_url + "/api/v2/auth/login",
                                     data={"username": self.config["username"], "password": self.config["password"]})
            if response.status_code != 200 or response.text.strip() != "Ok.":
                raise DownloadError("下载器认证失败，请检查连接配置", code="authentication", infrastructure=True)
            self._authenticated = True
        response = self._request("POST" if mutation else "GET", self.base_url + "/api/v2/" + method,
                                 data=data, params=params, files=files, mutation=mutation)
        if response.status_code == 404 and fallback:
            return self._call(fallback, data=data, mutation=mutation)
        if response.status_code >= 400:
            if method == "torrents/add":
                raise DownloadError("下载器拒绝此种子", code="invalid_resource")
            raise DownloadError("下载器拒绝操作", code="request_rejected", infrastructure=True)
        return response

    def _list(self, **params):
        try:
            result = self._call("torrents/info", params=params).json()
            if not isinstance(result, list):
                raise ValueError()
            return result
        except ValueError:
            raise DownloadError("下载器返回无效响应", code="response", infrastructure=True) from None

    def reconcile(self, attempt_key, candidate=None):
        tag = self.external_id(attempt_key)
        entries = self._list(tag=tag)
        for entry in entries:
            if tag in [t.strip() for t in str(entry.get("tags") or "").split(",")]:
                return self._status(entry)
        return None

    def submit(self, candidate, destination, attempt_key):
        existing = self.reconcile(attempt_key, candidate)
        if existing:
            return existing
        destination = _check_destination(destination)
        files = None
        data = {"savepath": destination, "tags": self.external_id(attempt_key), "autoTMM": "false"}
        try:
            if candidate.get("kind") == "magnet":
                infohash = magnet_infohash(candidate["url"])
                # Drop URL-bearing magnet extras (web seeds, exact sources, peer
                # hints and trackers). DHT retrieves metadata without allowing a
                # page to ask a privileged daemon to HTTP-fetch arbitrary URLs.
                prefix = "urn:btih:" if len(infohash) == 40 else "urn:btmh:1220"
                data["urls"] = "magnet:?xt=" + prefix + infohash
            elif candidate.get("kind") == "torrent":
                blob = fetch_torrent(candidate["url"])
                infohash = torrent_infohash(blob)
                files = {"torrents": ("resource.torrent", blob, "application/x-bittorrent")}
            else:
                raise SourceError("qBittorrent 仅支持磁力链接和种子")
            if candidate.get("infohash") and candidate["infohash"] != infohash:
                raise SourceError("种子内容已改变，请重新搜索资源")
        except (SourceError, KeyError):
            raise DownloadError("资源无效或种子内容已改变", code="invalid_resource") from None
        # Never adopt or stop an unrelated existing torrent belonging to another
        # task or a human. qB deduplicates infohash globally, not by save path.
        identity = infohash if len(infohash) == 40 else infohash + "|" + infohash[:40]
        if self._list(hashes=identity):
            raise DownloadError("此种子已被其他下载任务使用", code="duplicate_resource")
        response = self._call("torrents/add", data=data, files=files, mutation=True)
        if response.text.strip() not in ("", "Ok."):
            raise DownloadError("下载器未接受此种子", code="invalid_resource")
        result = self.reconcile(attempt_key, candidate)
        if result is None:
            raise DownloadError("下载已提交，等待下载器确认", code="submission_pending", infrastructure=True, uncertain=True)
        return result

    def _status(self, entry):
        raw_state = str(entry.get("state") or "unknown")
        completed = float(entry.get("progress") or 0) >= 1
        if raw_state in ("error", "missingFiles", "unknown"):
            state = "failed"
        elif raw_state.startswith("checking") or raw_state in ("allocating", "moving"):
            state = "checking"
        elif completed:
            state = "completed"
        elif raw_state in ("pausedDL", "pausedUP", "stoppedDL", "stoppedUP"):
            state = "paused"
        elif raw_state in ("queuedDL", "queuedUP"):
            state = "queued"
        else:
            state = "downloading"
        result = _empty(str(entry.get("hash") or ""), state)
        total = _number(entry.get("total_size") or entry.get("size"))
        downloaded = total if state == "completed" else _number(entry.get("completed", entry.get("downloaded")))
        result.update(downloaded_bytes=downloaded, total_bytes=total,
                      speed=_number(entry.get("dlspeed")), raw_state=raw_state,
                      infrastructure=state == "failed", error="下载器文件或存储状态异常" if state == "failed" else "")
        if state == "completed":
            try:
                files = self._call("torrents/files", params={"hash": result["external_id"]}).json()
                if not isinstance(files, list):
                    raise ValueError()
                result["files"] = [{"path": posixpath.join(str(entry.get("save_path") or ""), str(f.get("name") or "")),
                                    "size": _number(f.get("size"))} for f in files if _number(f.get("priority", 1)) > 0]
            except ValueError:
                raise DownloadError("下载器返回无效文件列表", code="response", infrastructure=True) from None
        return result

    def poll(self, external_id):
        entry = self._owned_entry(external_id)
        return self._status(entry) if entry else _empty(external_id, "missing")

    def _owned_entry(self, external_id):
        value = self._hash(external_id)
        entries = self._list(hashes=value)
        entry = next((e for e in entries if str(e.get("hash") or "").lower() == value.lower()), None)
        if entry is not None and self.config.get("attempt_key"):
            expected_tag = self.external_id(self.config["attempt_key"])
            tags = [tag.strip() for tag in str(entry.get("tags") or "").split(",")]
            if expected_tag not in tags:
                raise DownloadError("此下载已不属于当前任务，请检查下载器中的任务记录",
                                    code="ownership_conflict", infrastructure=True)
        return entry

    def _owned_hash(self, external_id):
        value = self._hash(external_id)
        if not self.config.get("attempt_key"):
            return value
        return value if self._owned_entry(value) is not None else None

    @staticmethod
    def _hash(external_id):
        if not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", str(external_id)):
            raise DownloadError("无效的下载标识", code="invalid_id")
        return str(external_id)

    def retry(self, external_id):
        value = self._hash(external_id)
        self._call("torrents/start", data={"hashes": value}, mutation=True, fallback="torrents/resume")
        self._call("torrents/reannounce", data={"hashes": value}, mutation=True)
        return self.poll(value)

    def pause(self, external_id):
        self._call("torrents/stop", data={"hashes": self._hash(external_id)}, mutation=True, fallback="torrents/pause")

    def cancel(self, external_id):
        self._call("torrents/delete", data={"hashes": self._hash(external_id), "deleteFiles": "false"}, mutation=True)

    def finish(self, external_id):
        self.cancel(external_id)


class Aria2Downloader(_Endpoint):
    kind = "aria2"

    def __init__(self, config):
        super().__init__(config)
        self.rpc_url = self.base_url if self.base_url.endswith("/jsonrpc") else self.base_url + "/jsonrpc"

    @staticmethod
    def external_id(attempt_key):
        return hashlib.sha256(str(attempt_key).encode()).hexdigest()[:16]

    @staticmethod
    def _gid(external_id):
        if not re.fullmatch(r"[a-fA-F0-9]{16}", str(external_id)) or set(str(external_id)) == {"0"}:
            raise DownloadError("无效的下载标识", code="invalid_id")
        return str(external_id)

    def _rpc(self, method, params=None, *, mutation=False, missing_ok=False):
        values = list(params or [])
        if self.config["secret"]:
            values.insert(0, "token:" + self.config["secret"])
        response = self._request("POST", self.rpc_url, mutation=mutation,
                                 json={"jsonrpc": "2.0", "id": "lingxi", "method": "aria2." + method, "params": values})
        if response.status_code >= 400:
            raise DownloadError("下载器拒绝操作", code="request_rejected", infrastructure=True)
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError()
        except ValueError:
            raise DownloadError("下载器返回无效响应", code="response", infrastructure=True, uncertain=mutation) from None
        if payload.get("error"):
            message = str(payload["error"].get("message") or "").lower()
            if missing_ok and ("not found" in message or "cannot find" in message):
                return None
            code = "authentication" if "unauthorized" in message else "rpc_error"
            raise DownloadError("下载器认证失败" if code == "authentication" else "下载器 RPC 操作失败",
                                code=code, infrastructure=True, uncertain=mutation)
        if "result" not in payload:
            raise DownloadError("下载器返回无效响应", code="response", infrastructure=True, uncertain=mutation)
        return payload["result"]

    def reconcile(self, attempt_key, candidate=None):
        result = self.poll(self.external_id(attempt_key))
        return None if result["state"] == "missing" else result

    def submit(self, candidate, destination, attempt_key):
        gid = self.external_id(attempt_key)
        existing = self.reconcile(attempt_key, candidate)
        if existing:
            return existing
        if candidate.get("kind") != "http":
            raise DownloadError("aria2 适配器用于 HTTP(S) 下载；磁力和种子请配置 qBittorrent", code="unsupported_resource")
        try:
            url = validate_public_url(_http_url(candidate["url"]))
        except (ValueError, KeyError):
            raise DownloadError("下载地址未通过公网安全校验", code="invalid_resource") from None
        filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        filename = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", filename).strip(" .")[:180] or "download.bin"
        options = {"gid": gid, "dir": _check_destination(destination), "out": filename,
                   "continue": "true", "allow-overwrite": "false", "auto-file-renaming": "false",
                   "max-tries": "1", "timeout": "60", "connect-timeout": "15",
                   "follow-torrent": "false", "follow-metalink": "false", "max-connection-per-server": "2"}
        returned = self._rpc("addUri", [[url], options], mutation=True)
        if returned != gid:
            raise DownloadError("下载器未确认预留下载标识", code="submission_pending", infrastructure=True, uncertain=True)
        return self.poll(gid)

    def poll(self, external_id):
        gid = self._gid(external_id)
        data = self._rpc("tellStatus", [gid], missing_ok=True)
        if data is None:
            return _empty(gid, "missing")
        if not isinstance(data, dict):
            raise DownloadError("下载器返回无效状态", code="response", infrastructure=True)
        raw = str(data.get("status") or "")
        state = {"active": "downloading", "waiting": "queued", "paused": "paused", "complete": "completed",
                 "error": "failed", "removed": "missing"}.get(raw, "checking")
        if data.get("verifyIntegrityPending") == "true" or data.get("verifiedLength") is not None:
            state = "checking" if raw == "active" else state
        result = _empty(gid, state)
        code = str(data.get("errorCode") or "")
        # Storage/file-system/daemon failures must wait for infrastructure instead
        # of consuming the user's alternative-source search budget.
        infrastructure = state == "failed" and code in ("9", "13", "14", "15", "16", "17", "18")
        result.update(downloaded_bytes=_number(data.get("completedLength")), total_bytes=_number(data.get("totalLength")),
                      speed=_number(data.get("downloadSpeed")), raw_state=raw, error_code=code,
                      infrastructure=infrastructure,
                      error=("下载存储或文件状态异常" if infrastructure else "资源下载失败") if state == "failed" else "",
                      files=[{"path": str(f.get("path") or ""), "size": _number(f.get("length"))}
                             for f in data.get("files", []) if f.get("selected", "true") == "true"])
        return result

    def retry(self, external_id):
        gid = self._gid(external_id)
        status = self.poll(gid)
        if status["state"] in ("missing", "failed"):
            # A stopped/error aria2 record cannot be unpaused. Return a typed
            # error so the worker can resubmit the same candidate under a fresh
            # *persisted* attempt key; never erase history before that commit.
            raise DownloadError("此下载已结束，需要创建同资源重试记录", code="retry_new_attempt")
        if status["state"] == "completed":
            return status
        self._rpc("forcePause", [gid], mutation=True)
        self._rpc("unpause", [gid], mutation=True)
        return self.poll(gid)

    def pause(self, external_id):
        self._rpc("forcePause", [self._gid(external_id)], mutation=True, missing_ok=True)

    def cancel(self, external_id):
        gid = self._gid(external_id)
        status = self.poll(gid)
        if status["state"] in ("failed", "completed", "missing"):
            self._rpc("removeDownloadResult", [gid], mutation=True, missing_ok=True)
        else:
            self._rpc("forceRemove", [gid], mutation=True, missing_ok=True)

    def finish(self, external_id):
        self._rpc("removeDownloadResult", [self._gid(external_id)], mutation=True, missing_ok=True)


def get_downloader(config: dict):
    kind = str(config.get("kind") or "").lower()
    if kind == "qbittorrent":
        return QBittorrentDownloader(config)
    if kind == "aria2":
        return Aria2Downloader(config)
    raise DownloadError("未配置合适的下载器", code="config", infrastructure=True)
