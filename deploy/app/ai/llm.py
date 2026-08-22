"""LLM 客户端封装（OpenAI 兼容协议，默认 DeepSeek）。

- 配置优先级：设置表（网页「设置 → AI 设置」可改）> .env / 应用 config > 默认值
- 每次调用时读取最新配置，配置变更后自动重建客户端，无需重启
- 所有构造 / 调用异常统一转为 LLMError（信息截断 300 字，回给调用方）
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT = 90


class LLMError(Exception):
    """LLM 调用异常。"""


def _attr(obj, key, default=None):
    """兼容属性 / 字典两种访问方式（openai 包版本差异）。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class LLMClient:
    def __init__(self, app=None):
        self.app = app
        self._client = None
        self._sig = None
        self.base_url = ""
        self.api_key = ""
        self.model = ""
        self.timeout = float(DEFAULT_TIMEOUT)

    # ---------- 配置读取（DB 设置 > .env > 默认值）----------
    def _read_config(self) -> dict:
        from app.services.settings_service import get_setting_from

        base = str(
            get_setting_from("llm_base_url", "LLM_BASE_URL", DEFAULT_BASE_URL) or ""
        ).strip().rstrip("/")
        key = str(get_setting_from("llm_api_key", "LLM_API_KEY", "") or "").strip()
        model = str(get_setting_from("llm_model", "LLM_MODEL", DEFAULT_MODEL) or "").strip()
        try:
            timeout = float(get_setting_from("llm_timeout", "LLM_TIMEOUT", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = float(DEFAULT_TIMEOUT)
        return {"base_url": base, "api_key": key, "model": model, "timeout": timeout}

    @property
    def is_configured(self) -> bool:
        return bool(self._read_config()["api_key"])

    # ---------- 客户端（配置变化时自动重建）----------
    @property
    def client(self):
        cfg = self._read_config()
        sig = (cfg["base_url"], cfg["api_key"], cfg["model"], cfg["timeout"])
        if self._client is None or sig != self._sig:
            self.base_url = cfg["base_url"]
            self.api_key = cfg["api_key"]
            self.model = cfg["model"]
            self.timeout = cfg["timeout"]
            self._client = self._build(cfg)
            self._sig = sig
        return self._client

    def _build(self, cfg: dict):
        try:
            from openai import OpenAI

            return OpenAI(
                api_key=cfg["api_key"],
                base_url=cfg["base_url"],
                timeout=cfg["timeout"],
            )
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"初始化 LLM 客户端失败: {str(e)[:300]}") from e

    # ---------- 调用 ----------
    def chat_stream(self, messages, tools=None):
        """流式对话，生成器。

        yield 字典：
          {"type": "delta", "text": str}            文本增量
          {"type": "tool_calls", "calls": [...]}    流结束后累积的工具调用
        calls 元素：{"id": str, "name": str, "arguments": str}
        """
        try:
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools or None,
                stream=True,
                temperature=0.7,
            )
            acc: dict[int, dict] = {}  # index → {id, name, arguments}
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
                            slot["name"] += str(name)
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
        except Exception as e:  # noqa: BLE001 —— 统一转 LLMError
            raise LLMError(str(e)[:300]) from e

    def chat(self, messages, tools=None):
        """非流式对话，返回 (content_str, tool_calls_list)。

        tool_calls_list 元素：{"id": str, "name": str, "arguments": str}
        """
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
        except Exception as e:  # noqa: BLE001 —— 统一转 LLMError
            raise LLMError(str(e)[:300]) from e
