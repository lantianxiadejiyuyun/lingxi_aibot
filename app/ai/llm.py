"""LLM 客户端：按用户隔离 API Key，支持 OpenAI 兼容 与 Anthropic Messages 两种协议。

- Key / 协议 / 地址 / 模型只读**当前用户**自己的设置，不回退全局、不共用 .env
- OpenAI 协议：官方 OpenAI、DeepSeek、通义、Kimi、Ollama、各类中转（chat.completions）
- Anthropic 协议：官方 Claude 或兼容网关（/v1/messages，含 tool_use）
- 内部消息仍是 OpenAI chat 格式；Anthropic 在边界转换
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

PROTOCOL_OPENAI = "openai"
PROTOCOL_ANTHROPIC = "anthropic"
PROTOCOLS = (PROTOCOL_OPENAI, PROTOCOL_ANTHROPIC)

DEFAULT_TIMEOUT = 90
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_MAX_TOKENS = 8192

# 快捷预设：设置页一键填入官方/常用 Base URL（Key 仍需用户自己贴）
# group: 国内 / 国际 / 本地；protocol: openai | anthropic
# 模型名为该平台常用对话模型，可在填入后自行改掉。
PROVIDER_PRESETS: list[dict[str, str]] = [
    # ---- 国内 ----
    {
        "id": "deepseek", "label": "DeepSeek", "group": "国内",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "hint": "深度求索 · OpenAI 兼容",
    },
    {
        "id": "qwen", "label": "通义千问", "group": "国内",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "hint": "阿里云百炼北京 · 国际站改 dashscope-intl.aliyuncs.com",
    },
    {
        "id": "kimi", "label": "Kimi", "group": "国内",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://api.moonshot.cn/v1",
        "model": "kimi-k2-turbo-preview",
        "hint": "月之暗面 Moonshot",
    },
    {
        "id": "glm", "label": "智谱 GLM", "group": "国内",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4.5",
        "hint": "BigModel 开放平台 · 路径是 /paas/v4 不是 /v1",
    },
    {
        "id": "doubao", "label": "豆包", "group": "国内",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-pro-32k",
        "hint": "火山方舟 · 模型建议改成你的推理接入点 ID（ep-…）",
    },
    {
        "id": "siliconflow", "label": "硅基流动", "group": "国内",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V3",
        "hint": "聚合多家开源模型",
    },
    {
        "id": "spark", "label": "讯飞星火", "group": "国内",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://spark-api-open.xf-yun.com/v1",
        "model": "generalv3.5",
        "hint": "讯飞开放平台 HTTP OpenAPI",
    },
    {
        "id": "hunyuan", "label": "腾讯混元", "group": "国内",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "model": "hunyuan-turbo",
        "hint": "腾讯云混元 OpenAI 兼容",
    },
    {
        "id": "minimax", "label": "MiniMax", "group": "国内",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://api.minimaxi.com/v1",
        "model": "MiniMax-Text-01",
        "hint": "海螺 / MiniMax 开放平台",
    },
    {
        "id": "stepfun", "label": "阶跃星辰", "group": "国内",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://api.stepfun.com/v1",
        "model": "step-2-mini",
        "hint": "StepFun",
    },
    {
        "id": "yi", "label": "零一万物", "group": "国内",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://api.lingyiwanwu.com/v1",
        "model": "yi-lightning",
        "hint": "Yi / Lingyiwanwu",
    },
    {
        "id": "baichuan", "label": "百川", "group": "国内",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://api.baichuan-ai.com/v1",
        "model": "Baichuan4-Turbo",
        "hint": "百川智能",
    },
    {
        "id": "ernie", "label": "文心一言", "group": "国内",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://qianfan.baidubce.com/v2",
        "model": "ernie-4.5-turbo-128k",
        "hint": "百度千帆 v2 OpenAI 兼容",
    },
    # ---- 国际 ----
    {
        "id": "openai", "label": "OpenAI", "group": "国际",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "hint": "官方 chat.completions",
    },
    {
        "id": "anthropic", "label": "Anthropic Claude", "group": "国际",
        "protocol": PROTOCOL_ANTHROPIC,
        "base_url": "https://api.anthropic.com",
        "model": "claude-sonnet-4-5",
        "hint": "官方 Messages API（不是 OpenAI 兼容）",
    },
    {
        "id": "xai", "label": "xAI Grok", "group": "国际",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://api.x.ai/v1",
        "model": "grok-4.6",
        "hint": "Grok · OpenAI 兼容",
    },
    {
        "id": "gemini", "label": "Google Gemini", "group": "国际",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
        "hint": "Gemini 的 OpenAI 兼容层",
    },
    {
        "id": "groq", "label": "Groq", "group": "国际",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "hint": "Groq 高速推理",
    },
    {
        "id": "openrouter", "label": "OpenRouter", "group": "国际",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4o-mini",
        "hint": "一个 Key 调多家模型，模型名带厂商前缀",
    },
    {
        "id": "mistral", "label": "Mistral", "group": "国际",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-small-latest",
        "hint": "Mistral 官方",
    },
    # ---- 本地 ----
    {
        "id": "ollama", "label": "Ollama", "group": "本地",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "llama3.1",
        "hint": "本机 Ollama · Key 可填 ollama",
    },
    {
        "id": "lmstudio", "label": "LM Studio", "group": "本地",
        "protocol": PROTOCOL_OPENAI,
        "base_url": "http://127.0.0.1:1234/v1",
        "model": "local-model",
        "hint": "本机 LM Studio 本地服务",
    },
]


def grouped_provider_presets() -> list[tuple[str, list[dict[str, str]]]]:
    """按国内 / 国际 / 本地分组，供设置页渲染。"""
    order = ("国内", "国际", "本地")
    buckets: dict[str, list[dict[str, str]]] = {g: [] for g in order}
    for p in PROVIDER_PRESETS:
        buckets.setdefault(p.get("group") or "其他", []).append(p)
    return [(g, buckets[g]) for g in order if buckets.get(g)]

class LLMError(Exception):
    """LLM 调用异常。"""


def normalize_protocol(value) -> str:
    raw = str(value or "").strip().lower()
    if raw in ("anthropic", "claude", "messages"):
        return PROTOCOL_ANTHROPIC
    return PROTOCOL_OPENAI


def default_for_protocol(protocol: str) -> dict[str, str]:
    proto = normalize_protocol(protocol)
    if proto == PROTOCOL_ANTHROPIC:
        p = next(x for x in PROVIDER_PRESETS if x["id"] == "anthropic")
    else:
        p = next(x for x in PROVIDER_PRESETS if x["id"] == "deepseek")
    return {"protocol": p["protocol"], "base_url": p["base_url"], "model": p["model"]}


def anthropic_messages_url(base_url: str) -> str:
    """把用户填的地址规范成 .../v1/messages。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        base = "https://api.anthropic.com"
    if base.endswith("/messages"):
        return base
    if base.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


def openai_tools_to_anthropic(tools: list | None) -> list[dict]:
    """OpenAI tools=[{type:function,function:{name,description,parameters}}] → Anthropic tools。"""
    out: list[dict] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if t.get("type") == "function" or "function" in t else t
        if not isinstance(fn, dict):
            fn = t
        name = (fn.get("name") or t.get("name") or "").strip()
        if not name:
            continue
        schema = fn.get("parameters") or t.get("input_schema") or {"type": "object", "properties": {}}
        out.append({
            "name": name,
            "description": fn.get("description") or t.get("description") or "",
            "input_schema": schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
        })
    return out


def _as_text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _merge_anthropic_message(out: list[dict], role: str, content) -> None:
    """追加或合并到上一条同角色消息（Anthropic 要求 user/assistant 严格交替）。"""
    if not out or out[-1]["role"] != role:
        out.append({"role": role, "content": content})
        return
    prev = out[-1]["content"]
    if isinstance(prev, str) and isinstance(content, str):
        out[-1]["content"] = (prev + "\n" + content).strip()
        return
    prev_blocks = prev if isinstance(prev, list) else ([_as_text_block(prev)] if prev else [])
    new_blocks = content if isinstance(content, list) else ([_as_text_block(str(content))] if content else [])
    out[-1]["content"] = prev_blocks + new_blocks


def to_anthropic_payload(messages: list, tools: list | None = None) -> dict:
    """OpenAI chat messages → Anthropic Messages 请求体（不含 model / stream / max_tokens）。"""
    system_parts: list[str] = []
    out: list[dict] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "").strip()
        if role == "system":
            text = m.get("content") or ""
            if text:
                system_parts.append(str(text))
            continue
        if role == "user":
            _merge_anthropic_message(out, "user", m.get("content") or "")
            continue
        if role == "assistant":
            blocks: list[dict] = []
            text = m.get("content") or ""
            if text:
                blocks.append(_as_text_block(str(text)))
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                raw_args = fn.get("arguments") if fn else tc.get("arguments") or "{}"
                if isinstance(raw_args, dict):
                    parsed = raw_args
                else:
                    try:
                        parsed = json.loads(raw_args or "{}")
                    except (TypeError, ValueError):
                        parsed = {}
                    if not isinstance(parsed, dict):
                        parsed = {}
                name = (fn.get("name") if fn else None) or tc.get("name") or ""
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id") or "",
                    "name": name,
                    "input": parsed,
                })
            if blocks:
                _merge_anthropic_message(out, "assistant", blocks)
            continue
        if role == "tool":
            _merge_anthropic_message(out, "user", [{
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id") or "",
                "content": str(m.get("content") or ""),
            }])
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": "（继续）"})
    if not out:
        out.append({"role": "user", "content": "你好"})
    body: dict = {"messages": out}
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    converted = openai_tools_to_anthropic(tools)
    if converted:
        body["tools"] = converted
    return body


def parse_anthropic_content(content) -> tuple[str, list[dict]]:
    """Anthropic content 块 → (文本, [{id,name,arguments}])。"""
    texts: list[str] = []
    calls: list[dict] = []
    if isinstance(content, str):
        return content, []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            texts.append(str(block.get("text") or ""))
        elif btype == "tool_use":
            raw_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            calls.append({
                "id": block.get("id") or "",
                "name": block.get("name") or "",
                "arguments": json.dumps(raw_input, ensure_ascii=False),
            })
    return "".join(texts), calls


def iter_anthropic_sse(events) -> list:
    """把 Anthropic SSE 事件（dict 或 JSON 字符串）收成与 chat_stream 相同的 yield 列表。

    供测试与流式解析共用。返回 ["delta"/tool_calls 事件...]。
    """
    text_parts: list[str] = []
    acc: dict[int, dict] = {}
    out: list[dict] = []
    for ev in events:
        if isinstance(ev, str):
            raw = ev.strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "content_block_start":
            idx = int(ev.get("index") or 0)
            block = ev.get("content_block") or {}
            if block.get("type") == "tool_use":
                acc[idx] = {
                    "id": block.get("id") or "",
                    "name": block.get("name") or "",
                    "arguments": "",
                }
        elif etype == "content_block_delta":
            idx = int(ev.get("index") or 0)
            delta = ev.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                piece = str(delta.get("text") or "")
                if piece:
                    text_parts.append(piece)
                    out.append({"type": "delta", "text": piece})
            elif dtype == "input_json_delta":
                slot = acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                slot["arguments"] += str(delta.get("partial_json") or "")
        elif etype == "content_block_stop":
            continue
    calls = [
        {"id": slot["id"], "name": slot["name"], "arguments": slot["arguments"] or "{}"}
        for _, slot in sorted(acc.items())
        if slot.get("name")
    ]
    if calls:
        out.append({"type": "tool_calls", "calls": calls})
    return out


def _attr(obj, key, default=None):
    """兼容属性 / 字典两种访问方式（openai 包版本差异）。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _http_error_message(resp) -> str:
    try:
        data = resp.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err)[:300]
        if isinstance(err, str):
            return err[:300]
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])[:300]
    except Exception:  # noqa: BLE001
        pass
    return (getattr(resp, "text", None) or str(resp.status_code))[:300]


class LLMClient:
    def __init__(self, app=None):
        self.app = app
        self._client = None
        self._sig = None
        self.protocol = PROTOCOL_OPENAI
        self.base_url = ""
        self.api_key = ""
        self.model = ""
        self.timeout = float(DEFAULT_TIMEOUT)

    def _read_config(self) -> dict:
        from app.services.settings_service import get_own_setting

        protocol = normalize_protocol(get_own_setting("llm_protocol", PROTOCOL_OPENAI))
        defaults = default_for_protocol(protocol)
        base = str(get_own_setting("llm_base_url", "") or "").strip().rstrip("/")
        model = str(get_own_setting("llm_model", "") or "").strip()
        key = str(get_own_setting("llm_api_key", "") or "").strip()
        if not base:
            base = defaults["base_url"]
        if not model:
            model = defaults["model"]
        try:
            timeout = float(get_own_setting("llm_timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = float(DEFAULT_TIMEOUT)
        return {
            "protocol": protocol,
            "base_url": base,
            "api_key": key,
            "model": model,
            "timeout": timeout,
        }

    @property
    def is_configured(self) -> bool:
        return bool(self._read_config()["api_key"])

    def _apply(self, cfg: dict) -> None:
        from app.utils.urlsafety import UrlSafetyError, check_provider_url

        try:
            cfg["base_url"] = check_provider_url(cfg["base_url"])
        except UrlSafetyError as e:
            raise LLMError(f"模型接口地址不安全：{e}") from e
        self.protocol = cfg["protocol"]
        self.base_url = cfg["base_url"]
        self.api_key = cfg["api_key"]
        self.model = cfg["model"]
        self.timeout = cfg["timeout"]

    @property
    def client(self):
        """OpenAI SDK 客户端（仅 OpenAI 协议）。Anthropic 走 HTTP，不使用此属性。"""
        cfg = self._read_config()
        self._apply(cfg)
        if cfg["protocol"] != PROTOCOL_OPENAI:
            return None
        sig = (cfg["protocol"], cfg["base_url"], cfg["api_key"], cfg["model"], cfg["timeout"])
        if self._client is None or sig != self._sig:
            self._client = self._build_openai(cfg)
            self._sig = sig
        return self._client

    def _build_openai(self, cfg: dict):
        try:
            from openai import OpenAI

            return OpenAI(
                api_key=cfg["api_key"],
                base_url=cfg["base_url"],
                timeout=cfg["timeout"],
            )
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"初始化 LLM 客户端失败: {str(e)[:300]}") from e

    def _require_key(self, cfg: dict) -> None:
        if not cfg.get("api_key"):
            raise LLMError("尚未配置 API Key，请在「设置 → 模型与人设」填写你自己的 Key")

    # ---------- 调用 ----------
    def chat_stream(self, messages, tools=None):
        """流式对话，生成器。

        yield 字典：
          {"type": "delta", "text": str}
          {"type": "tool_calls", "calls": [...]}   # {id,name,arguments}
        """
        cfg = self._read_config()
        self._apply(cfg)
        self._require_key(cfg)
        if cfg["protocol"] == PROTOCOL_ANTHROPIC:
            yield from self._anthropic_stream(cfg, messages, tools)
            return
        try:
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools or None,
                stream=True,
                temperature=0.7,
            )
            acc: dict[int, dict] = {}
            for chunk in stream:
                choices = _attr(chunk, "choices") or []
                if not choices:
                    continue
                delta = _attr(choices[0], "delta")
                if delta is None:
                    continue
                content = _attr(delta, "content")
                if content:
                    yield {"type": "delta", "text": str(content)}
                for tc in _attr(delta, "tool_calls") or []:
                    idx = _attr(tc, "index", 0)
                    if idx is None:
                        idx = 0
                    slot = acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    tc_id = _attr(tc, "id")
                    if tc_id:
                        slot["id"] = tc_id
                    fn = _attr(tc, "function")
                    if fn is not None:
                        name = _attr(fn, "name")
                        if name:
                            slot["name"] = str(name)
                        args = _attr(fn, "arguments")
                        if args:
                            slot["arguments"] += str(args)
            calls = [
                {"id": slot["id"], "name": slot["name"], "arguments": slot["arguments"]}
                for _, slot in sorted(acc.items())
                if slot.get("name")
            ]
            if calls:
                yield {"type": "tool_calls", "calls": calls}
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            raise LLMError(str(e)[:300]) from e

    def chat(self, messages, tools=None):
        """非流式对话，返回 (content_str, tool_calls_list)。"""
        cfg = self._read_config()
        self._apply(cfg)
        self._require_key(cfg)
        if cfg["protocol"] == PROTOCOL_ANTHROPIC:
            return self._anthropic_chat(cfg, messages, tools)
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools or None,
                stream=False,
                temperature=0.7,
            )
            choices = _attr(resp, "choices") or []
            if not choices:
                return "", []
            msg = _attr(choices[0], "message")
            content = _attr(msg, "content") or ""
            tool_calls = []
            for tc in _attr(msg, "tool_calls") or []:
                fn = _attr(tc, "function")
                tool_calls.append({
                    "id": _attr(tc, "id", ""),
                    "name": _attr(fn, "name", "") if fn is not None else "",
                    "arguments": _attr(fn, "arguments", "") if fn is not None else "",
                })
            return str(content), tool_calls
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            raise LLMError(str(e)[:300]) from e

    # ---------- Anthropic Messages API ----------
    def _anthropic_headers(self, api_key: str) -> dict:
        return {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            # 部分兼容网关只认 Bearer
            "authorization": f"Bearer {api_key}",
        }

    def _anthropic_body(self, cfg: dict, messages, tools, stream: bool) -> dict:
        body = to_anthropic_payload(messages, tools)
        body["model"] = cfg["model"]
        body["max_tokens"] = ANTHROPIC_MAX_TOKENS
        body["temperature"] = 0.7
        body["stream"] = stream
        return body

    def _anthropic_chat(self, cfg: dict, messages, tools) -> tuple[str, list[dict]]:
        import requests

        url = anthropic_messages_url(cfg["base_url"])
        try:
            resp = requests.post(
                url,
                headers=self._anthropic_headers(cfg["api_key"]),
                json=self._anthropic_body(cfg, messages, tools, stream=False),
                timeout=cfg["timeout"],
            )
        except requests.RequestException as e:
            raise LLMError(f"Anthropic 请求失败: {str(e)[:300]}") from e
        if resp.status_code >= 400:
            raise LLMError(_http_error_message(resp))
        try:
            data = resp.json()
        except ValueError as e:
            raise LLMError("Anthropic 返回了无法解析的响应") from e
        return parse_anthropic_content(data.get("content"))

    def _anthropic_stream(self, cfg: dict, messages, tools):
        import requests

        url = anthropic_messages_url(cfg["base_url"])
        timeout = (10, max(float(cfg["timeout"]), 120))
        try:
            resp = requests.post(
                url,
                headers=self._anthropic_headers(cfg["api_key"]),
                json=self._anthropic_body(cfg, messages, tools, stream=True),
                timeout=timeout,
                stream=True,
            )
        except requests.RequestException as e:
            raise LLMError(f"Anthropic 请求失败: {str(e)[:300]}") from e
        if resp.status_code >= 400:
            raise LLMError(_http_error_message(resp))

        acc: dict[int, dict] = {}
        try:
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if line.startswith("event:"):
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    ev = json.loads(payload)
                except ValueError:
                    continue
                etype = ev.get("type")
                if etype == "error":
                    err = ev.get("error") or {}
                    raise LLMError(str(err.get("message") or err or "Anthropic 流式错误")[:300])
                if etype == "content_block_start":
                    idx = int(ev.get("index") or 0)
                    block = ev.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        acc[idx] = {
                            "id": block.get("id") or "",
                            "name": block.get("name") or "",
                            "arguments": "",
                        }
                elif etype == "content_block_delta":
                    delta = ev.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        piece = str(delta.get("text") or "")
                        if piece:
                            yield {"type": "delta", "text": piece}
                    elif dtype == "input_json_delta":
                        idx = int(ev.get("index") or 0)
                        slot = acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        slot["arguments"] += str(delta.get("partial_json") or "")
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            raise LLMError(str(e)[:300]) from e
        finally:
            resp.close()

        calls = [
            {"id": slot["id"], "name": slot["name"], "arguments": slot["arguments"] or "{}"}
            for _, slot in sorted(acc.items())
            if slot.get("name")
        ]
        if calls:
            yield {"type": "tool_calls", "calls": calls}
