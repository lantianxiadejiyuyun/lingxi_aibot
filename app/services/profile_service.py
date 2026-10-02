"""用户私有的多组模型/提示词配置；凭证只在服务端解析。"""
from __future__ import annotations

import re
from uuid import uuid4
from urllib.parse import urlsplit

from app.services.settings_service import get_own_setting, get_setting, get_setting_from, set_setting
from app.utils.scoping import current_user_id

KINDS = ("chat", "prompt", "image", "vision")
STATE_KEYS = {"chat": "profile_id", "prompt": "prompt_profile_id",
              "image": "image_profile_id", "vision": "vision_profile_id"}


def _uid(user_id=None):
    uid = int(current_user_id() if user_id is None else user_id)
    if not uid:
        raise ValueError("请先登录后管理配置")
    return uid


def _catalog(kind, user_id=None):
    if kind not in KINDS:
        raise ValueError("未知配置类型")
    uid = _uid(user_id)
    value = get_own_setting(f"model_profiles:{kind}", {}, user_id=uid)
    value = value if isinstance(value, dict) else {}
    return {"active_id": value.get("active_id", "legacy"),
            "items": [dict(p) for p in value.get("items", []) if isinstance(p, dict)]}


def _legacy_config(kind, user_id):
    own = lambda key, default=None: get_own_setting(key, default, user_id=user_id)
    if kind == "chat":
        from app.ai.llm import default_for_protocol, normalize_protocol, DEFAULT_TIMEOUT

        protocol = normalize_protocol(own("llm_protocol", "openai"))
        defaults = default_for_protocol(protocol)
        try:
            timeout = float(own("llm_timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = float(DEFAULT_TIMEOUT)
        return {"protocol": protocol, "base_url": str(own("llm_base_url") or defaults["base_url"]).strip().rstrip("/"),
                "model": str(own("llm_model") or defaults["model"]).strip(),
                "api_key": str(own("llm_api_key", "") or "").strip(), "timeout": timeout,
                "models": own("llm_models", []), "reasoning_effort": own("llm_reasoning_effort", "default") or "default",
                "context_window_tokens": own("llm_context_window_tokens", 0),
                "vision_capability": own("llm_vision_capability", "auto")}
    if kind == "prompt":
        from app.ai.prompts import DEFAULT_ACK_TEMPLATE

        defaults = {"name": "灵犀", "preset": "default", "verbosity": "normal", "address": "",
                    "extra": "", "ack_template": DEFAULT_ACK_TEMPLATE, "ack_enabled": True}
        return {key: get_setting(f"ai_persona_{key}", value, user_id=user_id)
                for key, value in defaults.items()}
    # 保留旧部署的服务配置作为兼容组，新建的组只属于当前用户。
    cfg = {key: str(get_setting_from(f"{kind}_{key}", f"{kind.upper()}_{key.upper()}", "", user_id=user_id) or "").strip()
           for key in ("base_url", "api_key", "model")}
    cfg.update(protocol=own(f"{kind}_protocol", "openai"), timeout=90, reasoning_effort="default")
    if kind == "image":
        cfg["size"] = get_setting_from("image_size", "IMAGE_SIZE", "1024x1024", user_id=user_id)
    return cfg


def _conversation(conversation, uid):
    if conversation is None:
        from app.services.model_control_service import current_conversation

        try:
            conversation, _ = current_conversation()
        except ValueError:
            return None
    if conversation.user_id != uid:
        raise ValueError("无权访问此会话的配置")
    return conversation


def resolve_profile_config(kind, conversation=None, *, user_id=None, profile_id=None):
    """取得配置快照。显式会话覆盖 > 账号默认组 > 原有配置。"""
    if not (current_user_id() if user_id is None else user_id):
        if conversation is not None:
            raise ValueError("请先登录后访问会话配置")
        return {**_legacy_config(kind, 0), "profile_id": "legacy", "profile_name": "原有配置"}
    uid = _uid(user_id)
    catalog = _catalog(kind, uid)
    conv = _conversation(conversation, uid) if profile_id is None else None
    if conv is not None:
        state = get_own_setting(f"chat_llm:{conv.id}", {}, user_id=uid)
        if isinstance(state, dict):
            profile_id = state.get(STATE_KEYS[kind])
    chosen = profile_id or catalog["active_id"]
    item = next((p for p in catalog["items"] if p["id"] == chosen), None)
    if item is None and chosen != "legacy":
        item = next((p for p in catalog["items"] if p["id"] == catalog["active_id"]), None)
    if item:
        return {**item["config"], "profile_id": item["id"], "profile_name": item["name"]}
    return {**_legacy_config(kind, uid), "profile_id": "legacy", "profile_name": "原有配置"}


def profile_choices(kind, user_id=None):
    catalog = _catalog(kind, user_id)
    return [{"id": "legacy", "name": "原有配置"}] + [
        {"id": p["id"], "name": p["name"], "model": p["config"].get("model", ""),
         "context_window_tokens": p["config"].get("context_window_tokens", 0)}
        for p in catalog["items"]]


def profiles_view(user_id=None):
    result = {}
    for kind in KINDS:
        catalog = _catalog(kind, user_id)
        choices = profile_choices(kind, user_id)
        current = next((p for p in choices if p["id"] == catalog["active_id"]), choices[0])
        result[kind] = {"active_id": current["id"], "items": choices, "current": current}
    return result


def _checked_config(kind, config):
    cfg = dict(config)
    if kind == "prompt":
        from app.ai.prompts import PERSONA_PRESETS, VERBOSITY_HINTS

        if cfg.get("preset") not in PERSONA_PRESETS or cfg.get("verbosity") not in VERBOSITY_HINTS:
            raise ValueError("请选择有效的人设和回复详细度")
        for key, limit in {"name": 32, "address": 16, "extra": 20000, "ack_template": 80}.items():
            cfg[key] = str(cfg.get(key) or "").strip()
            if len(cfg[key]) > limit:
                raise ValueError(f"提示词字段 {key} 最多 {limit} 字")
        if cfg["preset"] == "custom" and not cfg["extra"]:
            raise ValueError("自定义提示词不能为空")
        cfg["name"] = cfg["name"] or "灵犀"
        cfg["ack_enabled"] = bool(cfg.get("ack_enabled"))
        return cfg
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or any(ord(c) < 33 for c in base)):
        raise ValueError("接口地址应为完整 HTTP(S) 地址，不包含账号、查询参数或片段")
    try:
        parsed.port
    except ValueError:
        raise ValueError("接口端口无效") from None
    cfg["base_url"] = base
    if cfg.get("protocol") not in ("openai", "anthropic"):
        raise ValueError("请选择有效的接口协议")
    from app.services.model_control_service import parse_model_list

    cfg["model"] = str(cfg.get("model") or "").strip()
    if not cfg["model"]:
        raise ValueError("模型名不能为空")
    parse_model_list([], cfg["model"])
    if kind == "chat":
        from app.ai.reasoning import validate_reasoning

        cfg["models"] = parse_model_list(cfg.get("models", []), cfg["model"])
        cfg["reasoning_effort"] = validate_reasoning(cfg.get("reasoning_effort", "default"), cfg["protocol"], base, cfg["model"])
        try:
            capacity = int(cfg.get("context_window_tokens") or 0)
        except (TypeError, ValueError):
            raise ValueError("上下文容量须填写 token 数") from None
        if capacity != 0 and not 32000 <= capacity <= 10000000:
            raise ValueError("模型上下文容量须为 32,000–10,000,000 tokens，或选择未指定")
        cfg["context_window_tokens"] = capacity
        if cfg.get("vision_capability", "auto") not in ("auto", "on", "off"):
            raise ValueError("视觉能力选项无效")
        cfg.setdefault("vision_capability", "auto")
    if kind == "image" and not re.fullmatch(r"\d{2,4}x\d{2,4}", str(cfg.get("size", ""))):
        raise ValueError("图片尺寸格式应为 宽x高，如 1024x1024")
    return cfg


def save_profile(kind, name, config, *, profile_id="legacy", save_as_new=False, clear_key=False, user_id=None):
    uid = _uid(user_id)
    catalog = _catalog(kind, uid)
    name = str(name or "").strip()
    if not name or len(name) > 60:
        raise ValueError("请填写 1–60 字的配置组名称")
    existing = next((p for p in catalog["items"] if p["id"] == profile_id), None)
    if profile_id != "legacy" and existing is None:
        raise ValueError("配置组不存在或不属于当前用户")
    previous = existing["config"] if existing else _legacy_config(kind, uid)
    cfg = _checked_config(kind, config)
    if kind != "prompt":
        supplied = str(cfg.get("api_key") or "").strip()
        # 空 Key 仅在同一接口保留；换厂商必须提供自己的 Key，避免误发旧凭证。
        if clear_key:
            cfg["api_key"] = ""
        elif supplied:
            cfg["api_key"] = supplied
        elif cfg["base_url"] == str(previous.get("base_url") or "").rstrip("/") and cfg["protocol"] == previous.get("protocol", "openai"):
            cfg["api_key"] = previous.get("api_key", "")
        else:
            raise ValueError("切换接口地址或协议时，请同时填写该服务的 API Key")
    is_new = save_as_new or existing is None
    if is_new and len(catalog["items"]) >= 30:
        raise ValueError("每类最多保存 30 组配置")
    chosen = uuid4().hex if is_new else profile_id
    item = {"id": chosen, "name": name, "config": cfg}
    catalog["items"] = [p for p in catalog["items"] if p["id"] != chosen] + [item]
    catalog["active_id"] = chosen
    set_setting(f"model_profiles:{kind}", catalog, user_id=uid)
    return chosen


def activate_profile(kind, profile_id, *, user_id=None):
    uid = _uid(user_id)
    catalog = _catalog(kind, uid)
    if profile_id not in {p["id"] for p in profile_choices(kind, uid)}:
        raise ValueError("配置组不存在或不属于当前用户")
    catalog["active_id"] = profile_id
    set_setting(f"model_profiles:{kind}", catalog, user_id=uid)


def delete_profile(kind, profile_id, *, user_id=None):
    uid = _uid(user_id)
    catalog = _catalog(kind, uid)
    if profile_id == "legacy" or not any(p["id"] == profile_id for p in catalog["items"]):
        raise ValueError("该配置组不能删除或不存在")
    catalog["items"] = [p for p in catalog["items"] if p["id"] != profile_id]
    if catalog["active_id"] == profile_id:
        catalog["active_id"] = "legacy"
    set_setting(f"model_profiles:{kind}", catalog, user_id=uid)
