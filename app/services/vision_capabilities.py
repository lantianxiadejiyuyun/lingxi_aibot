"""保守识别常见原生视觉模型；未知或私有别名由用户明确声明。

能力参考（2026-10-02）：
https://developers.openai.com/api/docs/models/gpt-5.1
https://cdn.openai.com/gpt-5-system-card.pdf
https://platform.claude.com/docs/en/build-with-claude/vision
https://ai.google.dev/gemini-api/docs/image-understanding
https://help.aliyun.com/zh/model-studio/vision/
"""
import re


def supports_native_vision(cfg):
    mode = cfg.get("vision_capability", "auto")
    if mode in ("on", "off"):
        return mode == "on"
    model = str(cfg.get("model") or "").lower().split("/")[-1]
    patterns = (
        r"gpt-4o(?:-mini)?(?:-\d{4}-\d{2}-\d{2})?",
        r"gpt-4\.1(?:-mini|-nano)?(?:-\d{4}-\d{2}-\d{2})?",
        r"gpt-4-turbo(?:-\d{4}-\d{2}-\d{2})?",
        r"gpt-5(?:-mini|-nano)?(?:-\d{4}-\d{2}-\d{2})?",
        r"gpt-5\.1(?:-\d{4}-\d{2}-\d{2})?",
        r"claude-3(?:[.-][57])?-(?:sonnet|haiku|opus)(?:-\d{8}|-latest)?",
        r"claude-(?:sonnet|opus|haiku)-4(?:-[056])?(?:-\d{8})?",
        r"gemini-(?:1\.5|2\.0|2\.5)-(?:pro|flash|flash-lite)(?:-latest|-\d{3})?",
        r"qwen-vl-(?:max|plus)(?:-latest|-\d{4}-\d{2}-\d{2})?",
        r"qwen3-vl-(?:plus|flash)(?:-latest|-\d{4}-\d{2}-\d{2})?",
        r"qwen(?:2\.5|3)-vl-\d+b(?:-a\d+b)?-(?:instruct|thinking)",
    )
    return any(re.fullmatch(pattern, model) for pattern in patterns)
