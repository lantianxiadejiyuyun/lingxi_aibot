"""Conservative provider-specific thinking controls, verified against official APIs.

Unknown gateways keep their default payload. Level names for token-budget APIs
are app presets (1,024 / 4,096 / 8,192 tokens), not vendor effort labels.

References (2026-10-01):
https://developers.openai.com/api/docs/guides/reasoning
https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/
https://www.alibabacloud.com/help/en/model-studio/deep-thinking
https://platform.claude.com/docs/en/build-with-claude/thinking
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

BUDGETS = {"low": 1024, "medium": 4096, "high": 8192}


def _family(protocol, base_url, model) -> str:
    host = (urlsplit(str(base_url or "")).hostname or "").lower()
    name = str(model or "").strip().lower()
    if protocol == "anthropic" and host == "api.anthropic.com":
        if re.fullmatch(r"claude-(?:opus|sonnet)-[45](?:-[5678])?(?:-\d{8})?", name):
            if re.match(r"claude-(?:opus|sonnet)-4(?:-5)?(?:-\d{8})?$", name):
                return "claude_manual"
            return "claude_adaptive"
        if re.fullmatch(r"claude-(?:haiku-4-5|3-7-sonnet)(?:-\d{8}|-latest)?", name):
            return "claude_manual"
        if re.fullmatch(r"claude-(?:fable|mythos)-(?:5(?:-1)?|preview)(?:-\d{8})?", name):
            return "claude_always"
    if protocol != "openai":
        return "unknown"
    if host == "api.openai.com":
        if re.fullmatch(r"gpt-6(?:\.1)?-(?:sol|luna|astra)(?:-\d{4}-\d{2}-\d{2})?", name):
            return "openai_six"
        if re.fullmatch(r"gpt-5(?:\.[1-6])?(?:-(?:mini|nano|pro))?(?:-\d{4}-\d{2}-\d{2})?", name):
            return "openai_five"
        if re.fullmatch(r"(?:o1|o3|o3-mini|o4-mini)(?:-\d{4}-\d{2}-\d{2})?", name):
            return "openai_o"
    if host == "api.deepseek.com" and name in {
        "deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
    }:
        return "deepseek"
    qwen_host = host in {
        "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com",
        "dashscope-us.aliyuncs.com", "coding.dashscope.aliyuncs.com",
    } or host.endswith(".maas.aliyuncs.com")
    if qwen_host:
        if name == "qwen3.8-omni-flash":
            return "qwen_effort"
        if "thinking" in name and name.startswith("qwen3-"):
            return "qwen_always"
        if name in {"qwen3.8-2.4t-a95b", "qwen3.7-max-preview", "qwen3.7-max-2026-05-17"}:
            return "qwen_always"
        if name in {"qwen-plus", "qwen-plus-latest", "qwen-flash", "qwen-turbo"}:
            return "qwen"
        dated = re.fullmatch(r"qwen-(plus|flash|turbo)-(\d{4}-\d{2}-\d{2})", name)
        if dated and dated[2] >= {"plus": "2025-04-28", "flash": "2025-07-28", "turbo": "2025-04-28"}[dated[1]]:
            return "qwen"
        if re.fullmatch(r"qwen3-(?:max(?:-preview|-\d{4}-\d{2}-\d{2})?|235b-a22b|32b|30b-a3b|14b|8b)", name):
            return "qwen"
        if re.fullmatch(r"qwen3\.[5-8]-(?:plus|max|flash|27b|397b-a17b|122b-a10b|35b-a3b)(?:-preview|-\d{4}-\d{2}-\d{2})?", name):
            return "qwen"
    return "unknown"


def reasoning_levels(protocol, base_url, model) -> list[str]:
    """Only return levels implemented by this exact provider/model combination."""
    family = _family(protocol, base_url, model)
    name = str(model or "").lower()
    if family == "deepseek":
        return ["default", "off", "low", "high", "max"]
    if family in {"qwen", "qwen_effort", "claude_manual"}:
        return ["default", "off", "low", "medium", "high"]
    if family in {"qwen_always", "claude_always", "openai_o"}:
        return ["default", "low", "medium", "high"]
    if family == "claude_adaptive":
        off = not name.startswith(("claude-opus-5-5", "claude-sonnet-5-5"))
        return ["default"] + (["off"] if off else []) + ["low", "medium", "high"]
    if family == "openai_six":
        off = name.startswith(("gpt-6-sol", "gpt-6-luna"))
        return ["default"] + (["off"] if off else []) + ["low", "medium", "high"]
    if family == "openai_five":
        if re.match(r"gpt-5-pro(?:-|$)", name):
            return ["default", "high"]
        if "-pro" in name:
            return ["default", "medium", "high"]
        off = bool(re.match(r"gpt-5\.[124](?:-|$)", name))
        return ["default"] + (["off"] if off else []) + ["low", "medium", "high"]
    return ["default"]


def validate_reasoning(level, protocol, base_url, model) -> str:
    level = str(level or "default").strip().lower()
    if level == "none":
        level = "off"
    supported = reasoning_levels(protocol, base_url, model)
    if level not in supported:
        raise ValueError(f"模型 {model} 在当前接口不支持思考等级 {level}；可选：{' / '.join(supported)}")
    return level


def request_options(cfg: dict, *, tools=False) -> dict:
    """SDK kwargs for OpenAI, body fields for Anthropic; no auth/address fields."""
    protocol, base, model = (cfg[k] for k in ("protocol", "base_url", "model"))
    level = validate_reasoning(cfg.get("reasoning_effort"), protocol, base, model)
    family = _family(protocol, base, model)
    name = model.lower()
    if family == "openai_six" and tools:
        if not (level == "off" and name.startswith(("gpt-6-sol", "gpt-6-luna"))):
            raise ValueError("该 GPT-6 模型的思考与工具调用需要 Responses API，当前 Chat Completions 接口不支持此组合，请切换模型")
    options: dict = {}
    # O-series/GPT-5/6 reject a custom temperature during reasoning. Claude's
    # recent models reject it even with thinking disabled. Omit conservatively.
    if family == "unknown":
        options["temperature"] = 0.7
    if level == "default":
        return options
    if family.startswith("openai_") or family == "qwen_effort":
        options["reasoning_effort"] = "none" if level == "off" else level
    elif family == "deepseek":
        options["extra_body"] = {"thinking": {"type": "disabled" if level == "off" else "enabled"}}
        if level != "off":
            options["reasoning_effort"] = level
    elif family in {"qwen", "qwen_always"}:
        extra = {} if family == "qwen_always" else {"enable_thinking": level != "off"}
        if level != "off":
            extra["thinking_budget"] = BUDGETS[level]
        options["extra_body"] = extra
    elif family.startswith("claude_"):
        if level == "off":
            options["thinking"] = {"type": "disabled"}
            if name.startswith("claude-opus-5"):
                options["output_config"] = {"effort": "high"}
        elif family == "claude_manual":
            options["thinking"] = {"type": "enabled", "budget_tokens": BUDGETS[level]}
            options["max_tokens"] = BUDGETS[level] + 8192
        else:
            options["thinking"] = {"type": "adaptive"}
            options["output_config"] = {"effort": level}
    return options


def requires_streaming(cfg: dict) -> bool:
    """Older Qwen open-weight thinking endpoints reject non-streaming requests."""
    family = _family(cfg["protocol"], cfg["base_url"], cfg["model"])
    return family in {"qwen", "qwen_always"} and bool(
        re.match(r"qwen3-\d", cfg["model"].lower())
    ) and cfg.get("reasoning_effort", "default") != "off"
