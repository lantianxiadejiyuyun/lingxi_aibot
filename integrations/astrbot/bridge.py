"""Lingxi's account-scoped HTTP bridge; no AstrBot dependency in this module."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit


PREFIX = "/integrations/astrbot"
CHUNK_SIZE = 1800
MAX_HISTORY = 512


class BridgeError(RuntimeError):
    def __init__(self, code="unavailable", status=0):
        # Never include upstream response bodies, URLs or credentials in errors.
        super().__init__(code)
        self.code = code
        self.status = status


def private_session(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        return False
    parts = value.split(":", 2)
    return (len(parts) == 3 and bool(parts[0]) and bool(parts[2])
            and parts[1] == "FriendMessage"
            and value == value.strip()
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


@dataclass(frozen=True)
class Account:
    name: str
    api_base: str
    api_token: str = field(repr=False)
    session: str

    @property
    def storage_key(self):
        identity = "\0".join((self.api_base, self.api_token, self.session))
        return "lingxi_bridge_" + hashlib.sha256(identity.encode()).hexdigest()


def parse_accounts(config):
    rows = config.get("accounts", [])
    if not isinstance(rows, list) or len(rows) > 20:
        raise ValueError("账号配置应为列表，最多 20 组")
    result, sessions, credentials = [], set(), set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 组账号配置无效")
        if row.get("enabled", True) is False:
            continue
        base = row.get("api_base", "")
        token = row.get("api_token", "")
        session = row.get("private_session", "")
        if not isinstance(base, str) or any(ord(c) < 33 for c in base):
            raise ValueError(f"第 {index} 组 API 地址无效")
        base = base.rstrip("/")
        try:
            url = urlsplit(base)
            valid_url = (url.scheme in {"http", "https"} and bool(url.hostname)
                         and not url.username and not url.password and not url.query
                         and not url.fragment and url.path.endswith(("/api/v1", "/api/v2"))
                         and len(base) <= 2048 and (url.port is None or url.port > 0))
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError(f"第 {index} 组地址须为完整的 HTTP(S) /api/v2（或旧版 /api/v1）地址")
        if (not isinstance(token, str) or not 1 <= len(token) <= 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise ValueError(f"第 {index} 组 API Token 无效")
        if not private_session(session):
            raise ValueError(f"第 {index} 组须填写 /lingxi 身份 返回的私聊标识")
        if session in sessions or (base, token) in credentials:
            raise ValueError("一个私聊只能绑定一个账号，一个账号只能配置一个私聊")
        sessions.add(session)
        credentials.add((base, token))
        result.append(Account(str(row.get("name") or f"账号 {index}"), base, token, session))
    return result


class Client:
    def __init__(self, account, http):
        self.account = account
        self.http = http

    async def request(self, method, path, body=None):
        kwargs = {"headers": {"Authorization": "Bearer " + self.account.api_token},
                  "follow_redirects": False, "timeout": 300 if path.endswith("/chat") else 30}
        if body is not None:
            kwargs["json"] = body
        try:
            response = await self.http.request(method, self.account.api_base + path, **kwargs)
            data = response.json()
        except Exception:
            raise BridgeError() from None
        if not isinstance(data, dict) or data.get("ok") is not True or not 200 <= response.status_code < 300:
            status = response.status_code
            raise BridgeError("unauthorized" if status == 401 else "request_failed", status)
        return data.get("data")

    async def binding(self):
        data = await self.request("GET", PREFIX + "/binding")
        if not isinstance(data, dict):
            raise BridgeError("invalid_response")
        if not data.get("bound"):
            return None
        if data.get("session") != self.account.session:
            raise BridgeError("binding_mismatch")
        conversation_id = data.get("conversation_id")
        valid_id = (isinstance(conversation_id, str) and 1 <= len(conversation_id) <= 64
                    if self.account.api_base.endswith("/api/v2") else
                    type(conversation_id) is int and conversation_id >= 1)
        if conversation_id is not None and not valid_id:
            raise BridgeError("invalid_response")
        return data

    async def bind(self):
        await self.request("PUT", PREFIX + "/binding", {"session": self.account.session})
        return await self.binding()

    async def unbind(self):
        # A stale local configuration must not delete a new recipient's binding.
        if await self.binding():
            await self.request("DELETE", PREFIX + "/binding", {"session": self.account.session})


def _trim(mapping, limit=MAX_HISTORY):
    while len(mapping) > limit:
        mapping.pop(next(iter(mapping)))


class Bridge:
    """Serializes each account and persists deduplication before side effects.

    load/save use AstrBot's plugin KV database. They never store tokens or text.
    One plugin instance should serve a configured account (not active-active).
    """

    def __init__(self, client, load, save):
        self.client = client
        self.load = load
        self.save = save
        self.lock = asyncio.Lock()

    async def state(self):
        state = await self.load()
        if not isinstance(state, dict):
            state = {}
        for key in ("incoming", "outgoing"):
            if not isinstance(state.get(key), dict):
                state[key] = {}
        return state

    async def chat(self, text, message_id, send):
        async with self.lock:
            binding = await self.client.binding()
            if not binding:
                raise BridgeError("not_bound")
            if binding.get("conversation_id") is None:
                raise BridgeError("conversation_missing")
            state = await self.state()
            key = hashlib.sha256(str(message_id).encode()).hexdigest() if message_id else None
            if key and key in state["incoming"]:
                return "duplicate"
            if key:
                state["incoming"][key] = "started"
                _trim(state["incoming"])
                # If this write fails, do not submit a potentially mutating AI request.
                await self.save(state)
            try:
                body = {"conversation_id": binding["conversation_id"], "message": text,
                        "stream": False}
                if key:
                    body["request_id"] = "astrbot-" + key
                elif self.client.account.api_base.endswith("/api/v2"):
                    body["request_id"] = "astrbot-" + uuid.uuid4().hex
                path = PREFIX + "/chat" if self.client.account.api_base.endswith("/api/v2") else "/chat"
                data = await self.client.request("POST", path, body)
                if (not isinstance(data, dict) or not isinstance(data.get("reply"), str)
                        or not data["reply"].strip()):
                    raise BridgeError("invalid_response")
                for offset in range(0, len(data["reply"]), CHUNK_SIZE):
                    if await send(data["reply"][offset:offset + CHUNK_SIZE]) is False:
                        raise BridgeError("send_failed")
            except BaseException:
                if key:
                    state["incoming"][key] = "failed"
                    await self.save(state)
                raise
            if key:
                state["incoming"][key] = "done"
                await self.save(state)
            return "sent"

    async def poll(self, send):
        async with self.lock:
            if not await self.client.binding():
                return
            state = await self.state()
            if self.client.account.api_base.endswith("/api/v2"):
                for ident, ack in list(state.get("acks", {}).items()):
                    await self.acknowledge(state, ident, ack)
            items = await self.client.request("POST", PREFIX + "/outbox/claim", {"limit": 1})
            if not isinstance(items, list) or len(items) > 10:
                raise BridgeError("invalid_response")
            for item in items:
                if (not isinstance(item, dict) or type(item.get("id")) is not int
                        or item["id"] < 1 or not isinstance(item.get("lease_token"), str)
                        or not item["lease_token"]):
                    raise BridgeError("invalid_response")
                ident = str(item["id"])
                ack = {"lease_token": item["lease_token"], "outcome": "failed"}
                text = item.get("text")
                if (item.get("session") != self.client.account.session
                        or not private_session(item.get("session"))
                        or not isinstance(text, str) or not text.strip() or len(text) > 12000):
                    await self.acknowledge(state, ident, ack)
                    continue
                digest = hashlib.sha256(text.encode()).hexdigest()
                previous = state["outgoing"].get(ident, {})
                if previous and previous.get("digest") != digest:
                    # A queue ID must never be reused for different content.
                    await self.acknowledge(state, ident, ack)
                    continue
                progress = previous.get("chunks", 0)
                chunks = [text[i:i + CHUNK_SIZE] for i in range(0, len(text), CHUNK_SIZE)]
                if type(progress) is not int or not 0 <= progress <= len(chunks):
                    raise BridgeError("invalid_state")
                try:
                    for index in range(progress, len(chunks)):
                        # Platforms without a matching adapter return False.
                        if await send(self.client.account.session, chunks[index]) is not True:
                            raise BridgeError("send_failed")
                        state["outgoing"][ident] = {"digest": digest, "chunks": index + 1}
                        _trim(state["outgoing"])
                        await self.save(state)
                    ack["outcome"] = "sent"
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Preserve completed chunks. The server backs off failed deliveries.
                    await self.acknowledge(state, ident, ack)
                    continue
                # An ACK loss retains completed chunks, so a new lease only retries ACK.
                await self.acknowledge(state, ident, ack)

    async def acknowledge(self, state, ident, ack):
        v2 = self.client.account.api_base.endswith("/api/v2")
        if v2:
            state.setdefault("acks", {})[ident] = ack
            await self.save(state)
        await self.client.request("POST", PREFIX + f"/outbox/{ident}/ack", ack)
        if v2:
            state["acks"].pop(ident, None)
            await self.save(state)
