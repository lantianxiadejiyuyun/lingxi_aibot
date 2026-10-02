"""Route images by the user's choice, not a stale model-name allowlist.

The provider response decides whether a model can read the supplied image.
Unknown model names and gateway aliases must get the same native attempt.
"""


def should_try_native_vision(cfg):
    """Auto/on try the conversation model; only explicit off skips it."""
    return cfg.get("vision_capability", "auto") != "off"
