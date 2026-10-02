"""Declared model context windows and approximate request budgeting.

These settings describe a provider/model's existing capacity. They do not
enable a larger context window at the provider, and token estimates are not
billing counts. The provider remains the authority for its actual limits.
"""
from __future__ import annotations

from collections.abc import Mapping


CONTEXT_WINDOW_OPTIONS = [
    (32000, "32K"), (64000, "64K"), (128000, "128K"),
    (256000, "256K"), (512000, "512K"),
    (1000000, "100万"), (2000000, "200万"),
]
DEFAULT_CONTEXT_WINDOW_TOKENS = 128000
OUTPUT_RESERVE_TOKENS = 8192
SYSTEM_TOOLS_RESERVE_TOKENS = 8192
IMAGE_ESTIMATE_TOKENS = 4096


class ContextWindowError(ValueError):
    """A request cannot safely fit the selected model's declared window."""


def estimate_tokens(value) -> int:
    """Estimate common text conservatively, with explicit multimodal handling.

    UTF-8 bytes / 2 intentionally leaves more room than common English prose
    estimates. Tokenizers, unusual Unicode, and image resolutions vary, so
    callers also reserve headroom. Base64 image bytes are never counted as text.
    """
    if value is None:
        return 0
    if isinstance(value, str):
        return (len(value.encode("utf-8")) + 1) // 2
    if isinstance(value, Mapping):
        if value.get("type") in ("image_url", "image", "input_image"):
            return IMAGE_ESTIMATE_TOKENS
        return 4 + sum(estimate_tokens(str(key)) + estimate_tokens(item)
                       for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return 2 + sum(estimate_tokens(item) for item in value)
    return estimate_tokens(str(value))


def context_budget(cfg) -> dict:
    """Return input and output budgets without granting provider capabilities.

    Configurations predating model profiles keep their previous compaction
    behavior until a context capacity is explicitly saved.
    """
    cfg = cfg if isinstance(cfg, Mapping) else {}
    declared = cfg.get("context_window_tokens")
    legacy = not isinstance(declared, bool) and declared in (None, 0, "0", "")
    if legacy:
        window = DEFAULT_CONTEXT_WINDOW_TOKENS
    else:
        if isinstance(declared, bool) or not str(declared).isdigit() or int(declared) <= 0:
            raise ValueError("模型上下文容量必须是正整数 token")
        window = int(declared)
    output = OUTPUT_RESERVE_TOKENS
    # Claude's manually enabled thinking increases max_tokens. Reserve the
    # actual configured generation budget, including those reasoning tokens.
    if all(key in cfg for key in ("protocol", "base_url", "model")):
        from app.ai.reasoning import request_options

        try:
            output = max(output, int(request_options(cfg).get("max_tokens", output)))
        except (TypeError, ValueError):
            pass  # Configuration validation is handled by the model client.
    request_budget = max(0, window - output)
    history_budget = max(0, request_budget - SYSTEM_TOOLS_RESERVE_TOKENS)
    return {
        "context_window_tokens": window,
        "input_budget_tokens": history_budget,
        "request_input_budget_tokens": request_budget,
        "output_reserve_tokens": output,
        "system_tools_reserve_tokens": SYSTEM_TOOLS_RESERVE_TOKENS,
        "auto_threshold_tokens": int(history_budget * 0.8),
        "legacy_context_policy": legacy,
        "token_estimate_is_approximate": True,
    }


def assert_context_fits(messages, tools=None, cfg=None) -> dict:
    """Check each complete request, including tool results and current model.

    Nothing is truncated or removed when this fails. The caller may compact
    eligible older turns or ask the user to select a genuinely larger model.
    """
    budget = context_budget(cfg)
    estimated = estimate_tokens(messages) + estimate_tokens(tools)
    report = {**budget, "request_estimated_tokens": estimated}
    if not budget["legacy_context_policy"] and estimated > budget["request_input_budget_tokens"]:
        raise ContextWindowError(
            f"当前模型设置的上下文窗口为 {budget['context_window_tokens']:,} token，"
            f"预留输出后可用输入约 {budget['request_input_budget_tokens']:,} token；"
            f"本次完整请求估算约 {estimated:,} token，无法容纳。"
            "请先压缩历史、缩短当前输入，或切换到实际支持更大上下文的模型。"
            "聊天记录已保留；调整容量档位不会扩展模型本身的上限。"
        )
    return report
