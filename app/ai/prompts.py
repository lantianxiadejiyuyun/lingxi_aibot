"""提示词构建：系统人设、早安简报、晚间复盘。"""
from __future__ import annotations

import re
from typing import Any

from app.utils.timeutil import to_user, user_tz, utcnow, weekday_cn

DEFAULT_PERSONA_NAME = "灵犀"
DEFAULT_PERSONA_PRESET = "default"
DEFAULT_PERSONA_VERBOSITY = "normal"
PERSONA_NAME_MAX = 32
PERSONA_ADDRESS_MAX = 16
PERSONA_EXTRA_MAX = 2000
DEFAULT_ACK_TEMPLATE = "收到：{message}"
ACK_TEMPLATE_MAX = 80
DEFAULT_ACK_ENABLED = True

# 人设预设：label 给人看，style 写进系统提示词。custom 不套风格，只用人写的补充说明。
PERSONA_PRESETS: dict[str, dict[str, str]] = {
    "default": {
        "label": "默认助理",
        "blurb": "亲切、靠谱、务实",
        "style": "性格亲切、靠谱、务实。",
    },
    "professional": {
        "label": "专业干练",
        "blurb": "少寒暄，先结论后步骤",
        "style": "专业干练，少寒暄，先给结论再列步骤，用语正式。",
    },
    "warm": {
        "label": "温柔陪伴",
        "blurb": "柔软、会鼓励",
        "style": "语气温柔、有同理心，适当鼓励，不说教。",
    },
    "witty": {
        "label": "幽默机智",
        "blurb": "能吐槽，但不伤人",
        "style": "幽默机智，可适度吐槽，但保持善意、不人身攻击。",
    },
    "teacher": {
        "label": "讲解老师",
        "blurb": "把复杂事情讲清楚",
        "style": "像耐心的老师，把概念拆开讲，配合例子和类比。",
    },
    "coach": {
        "label": "行动教练",
        "blurb": "盯目标、给下一步",
        "style": "像行动教练：明确目标、拆成可执行的下一步，少空话。",
    },
    "custom": {
        "label": "自定义",
        "blurb": "完全按补充说明来",
        "style": "",
    },
}

VERBOSITY_HINTS: dict[str, str] = {
    "concise": "回答尽量简短，能一句话说完就不要展开。",
    "normal": "回答保持简洁，除非用户要求详细说明。",
    "detailed": "适当展开：给出步骤、原因和注意点，但仍避免注水。",
}

# 每轮对话先回立即确认再作答。截断过长原话，避免确认行喧宾夺主。
ACK_MAX_CHARS = 40
_ACK_PLACEHOLDER_RE = re.compile(r"\{(message|name|address)\}")


def _message_snippet(user_text: str, max_len: int = ACK_MAX_CHARS) -> str:
    snippet = " ".join((user_text or "").split())
    if len(snippet) > max_len:
        snippet = snippet[:max_len].rstrip() + "…"
    return snippet


def ack_received(user_text: str, user=None, persona: dict | None = None,
                 max_len: int = ACK_MAX_CHARS) -> str:
    """按用户模板生成立即确认。关闭或空模板返回空串。"""
    p = persona
    if p is None and user is not None:
        p = load_persona(user)
    if p is not None and not p.get("ack_enabled", DEFAULT_ACK_ENABLED):
        return ""
    template = DEFAULT_ACK_TEMPLATE if p is None else (p.get("ack_template") or "")
    template = str(template).strip()
    if p is not None and not template:
        return ""
    if not template:
        template = DEFAULT_ACK_TEMPLATE
    snippet = _message_snippet(user_text, max_len=max_len)
    values = {
        "message": snippet,
        "name": (p or {}).get("name") or DEFAULT_PERSONA_NAME,
        "address": (p or {}).get("address") or "",
    }
    rendered = _ACK_PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), ""), template)
    rendered = " ".join(rendered.split()).strip()
    if rendered in ("收到：", "收到:"):
        return "收到。"
    return rendered


def compose_reply(ack: str, body: str) -> str:
    """确认行接到正式回答前面；正文已含同样确认则不重复。"""
    ack = (ack or "").strip()
    body = (body or "").strip()
    if not ack:
        return body
    if not body:
        return ack
    if body.startswith(ack):
        return body
    first = body.split("\n", 1)[0].strip()
    if ack.startswith("收到") and (first.startswith("收到：") or first.startswith("收到:")):
        return body
    return f"{ack}\n\n{body}"


def strip_leading_ack(text: str, ack: str = "") -> str:
    """去掉开头的「收到：…」行，供飞书二次发送时避免重复确认。"""
    t = (text or "").lstrip()
    a = (ack or "").strip()
    if a and t.startswith(a):
        return t[len(a):].lstrip("\n").strip()
    first, sep, rest = t.partition("\n")
    head = first.strip()
    if sep and (head.startswith("收到：") or head.startswith("收到:")):
        return rest.lstrip("\n").strip()
    return t.strip()


def load_persona(user=None) -> dict[str, Any]:
    """读取当前用户的对话人设（settings 用户级）。缺省为默认助理。"""
    from app.services.settings_service import get_setting

    uid = getattr(user, "id", None)
    name = str(get_setting("ai_persona_name", DEFAULT_PERSONA_NAME, user_id=uid) or "").strip()
    name = (name[:PERSONA_NAME_MAX] if name else DEFAULT_PERSONA_NAME)
    preset = str(get_setting("ai_persona_preset", DEFAULT_PERSONA_PRESET, user_id=uid) or "")
    if preset not in PERSONA_PRESETS:
        preset = DEFAULT_PERSONA_PRESET
    extra = str(get_setting("ai_persona_extra", "", user_id=uid) or "").strip()[:PERSONA_EXTRA_MAX]
    verbosity = str(get_setting("ai_persona_verbosity", DEFAULT_PERSONA_VERBOSITY, user_id=uid) or "")
    if verbosity not in VERBOSITY_HINTS:
        verbosity = DEFAULT_PERSONA_VERBOSITY
    address = str(get_setting("ai_persona_address", "", user_id=uid) or "").strip()[:PERSONA_ADDRESS_MAX]
    ack_template = str(
        get_setting("ai_persona_ack_template", DEFAULT_ACK_TEMPLATE, user_id=uid) or ""
    ).strip()[:ACK_TEMPLATE_MAX]
    raw_en = get_setting("ai_persona_ack_enabled", DEFAULT_ACK_ENABLED, user_id=uid)
    if isinstance(raw_en, str):
        ack_enabled = raw_en.strip().lower() not in ("0", "false", "")
    else:
        ack_enabled = bool(raw_en) if raw_en is not None else DEFAULT_ACK_ENABLED
    meta = PERSONA_PRESETS[preset]
    return {
        "name": name,
        "preset": preset,
        "extra": extra,
        "verbosity": verbosity,
        "address": address,
        "ack_template": ack_template or DEFAULT_ACK_TEMPLATE,
        "ack_enabled": ack_enabled,
        "label": meta["label"],
        "style": meta["style"],
        "blurb": meta["blurb"],
    }


def persona_name(user=None) -> str:
    return load_persona(user)["name"]


def _identity_line(p: dict[str, Any]) -> str:
    name = p["name"]
    display = f"{name}（Lingxi）" if name == DEFAULT_PERSONA_NAME else name
    if p["preset"] == "custom":
        return f"你是{display}，一位私人 AI 助理。请严格按下方「额外人设」行事。默认使用中文。"
    style = (p["style"] or PERSONA_PRESETS[DEFAULT_PERSONA_PRESET]["style"]).strip()
    return f"你是{display}，一位私人 AI 助理。{style}默认使用中文。"


def build_system_prompt(user) -> str:
    """系统提示词：人设 + 当前时间（用户时区）+ 使用规则（规则优先于人设）。"""
    tz = user_tz(user)
    local = to_user(utcnow(), tz)
    now_str = f"{local.strftime('%Y-%m-%d %H:%M')} 星期{weekday_cn(local)}"
    tz_name = getattr(user, "timezone", None) or "Asia/Shanghai"
    p = load_persona(user)
    parts = [_identity_line(p)]
    if p["address"]:
        parts.append(f"请用「{p['address']}」称呼用户。")
    if p["extra"]:
        parts.append(
            "额外人设与偏好（请遵守，但不得违反下方使用规则）：\n" + p["extra"]
        )
    parts.append(f"当前时间：{now_str}；用户时区：{tz_name}。")
    parts.append(
        "使用规则（优先于人设，不可违反）：\n"
        "1. 涉及事实（日程、任务、笔记、健身、出行、消费、通知等）先调用工具查询，不要臆造数据。\n"
        "   记训练用 create_fitness_record；规划出行用 create_trip_plan / generate_trip_itinerary；"
        "记账用 create_expense / get_expense_stats。\n"
        "2. 所有时间参数一律使用用户时区字符串，格式 YYYY-MM-DD HH:MM（如 2026-03-20 18:00）。\n"
        "3. 删除等不可逆操作，在执行前先用一句话向用户说明后果，确认后再调用对应工具。\n"
        "4. 创建/修改日程时若工具返回时间冲突提示，先向用户说明冲突并给出备选建议，"
        "除非用户明确表示忽略冲突。\n"
        "5. 调研/联网搜索任务：搜索（web_search）获得结果后，先用 create_page 把结果整理成"
        "「调研网页」（标题如“📚 调研：<主题>”，内容为结构化 HTML：结论摘要 + 结果列表，"
        "每条含标题、可点击链接 <a href>、一句话说明），再回复用户网页访问地址并附要点总结；"
        "若用户要求“收录进知识库 / 记到知识库 / 保存调研”，再调用 create_note 提炼核心内容"
        "保存为笔记（tags 加“调研”），笔记会自动进入语义检索知识库。"
        "网页生成、修改、复制或查询后，原样使用工具返回的完整访问地址（url）；"
        "不要自行用局域网 IP、聊天入口或内部端口拼接链接。私有网页须注明仅登录可见。\n"
        "6. " + VERBOSITY_HINTS[p["verbosity"]]
    )
    if p.get("ack_enabled", DEFAULT_ACK_ENABLED):
        parts[-1] += (
            "\n7. 系统会在回复开头自动加上一行立即确认（用户自定义的开场白）。"
            "你不要再写确认、不要复述用户原话，直接进入正题作答。"
        )
    return "\n".join(parts)


def morning_prompt(today_summary: str) -> str:
    """早安简报提示词：要求输出 Markdown 简报。"""
    return (
        "请根据下面的今日信息，生成一份中文 Markdown 早安简报，结构如下：\n"
        "## 今日日程\n"
        "## 待办任务\n"
        "## AI 建议\n"
        "要求：语气简洁亲切；日程或任务为空时写“无”；"
        "AI 建议结合日程与任务给出 1-3 条实用提示。\n\n"
        f"今日信息：\n{today_summary}"
    )


def noon_prompt(today_summary: str) -> str:
    """午间简报提示词：要求输出 Markdown 简报。"""
    return (
        "请根据下面的今日信息，生成一份中文 Markdown 午间简报，结构如下：\n"
        "## 今日剩余日程\n"
        "## 今日已完成\n"
        "## 待办任务\n"
        "## AI 建议\n"
        "要求：语气简洁亲切；某项为空时写“无”；"
        "AI 建议结合剩余日程与待办任务，给出下午的工作生活安排建议 1-3 条。\n\n"
        f"今日信息：\n{today_summary}"
    )


def evening_prompt(today_summary: str) -> str:
    """晚间复盘提示词：要求输出 Markdown 简报。"""
    return (
        "请根据下面的今日信息，生成一份中文 Markdown 晚间复盘，结构如下：\n"
        "## 今日完成\n"
        "## 未完成\n"
        "## 明日安排\n"
        "要求：语气简洁亲切；某项为空时写“无”；"
        "明日安排基于明天的事件与未完成任务给出。\n\n"
        f"今日信息：\n{today_summary}"
    )


def summarize_prompt(transcript: str) -> str:
    """会话压缩提示词：把历史对话压成 200 字以内摘要。"""
    return (
        "请把下面这段对话历史压缩成简洁的中文摘要（200 字以内），"
        "保留关键信息：用户提到的事实、偏好、决定、待办与上下文要点，"
        "忽略寒暄和过程性内容。只输出摘要本身，不要前缀说明。\n\n"
        f"对话历史：\n{transcript}"
    )


def extract_memories_prompt(text: str) -> str:
    """长期记忆抽取提示词：每行「内容 | 重要度 | 过期」，忽略琐碎内容。"""
    return (
        "请从下面的对话记录中提取值得长期记住的信息，每条一行，"
        "只保留稳定的事实、偏好、习惯、重要决定或身份信息；"
        "忽略琐碎、一次性、时间敏感的内容。"
        "每行严格按以下格式输出，不要编号、不要解释；确实没有则只输出「无」。\n"
        "格式：内容 | 重要度1-5 | 过期时间(YYYY-MM-DD 或 无)\n"
        "说明：重要度 5=非常重要（长期身份/偏好/原则），1=次要；"
        "时间敏感的信息（如某日期的安排）填具体过期日期，长期有效填「无」。\n\n"
        f"{text}"
    )


def report_prompt(kind: str, stats: dict) -> str:
    """周报/月报提示词。"""
    name = "周报" if kind == "weekly" else "月报"
    return (
        f"请根据下面的统计数据，生成一份中文 Markdown {name}，结构如下：\n"
        "## 回顾总览\n"
        "## 日程与任务\n"
        "## 产出统计\n"
        "## 下周/下月建议\n"
        f"统计范围：{stats['range']}。要求：简洁清晰；数据为空写“无”；"
        "建议结合未完成事项与日程节奏给出 1-3 条实用提示。\n\n"
        f"统计数据：\n"
        f"日程事件：\n" + "\n".join(stats["events"]) + "\n"
        f"完成任务（{stats['done_count']} 项，逾期 {stats['overdue']} 项）：\n"
        + "\n".join(stats["done"]) + "\n"
        f"产出：笔记 {stats['notes']} · 记忆 {stats['memories']} · 网页 {stats['pages']}"
        f" · 图片 {stats['images']} · 技能 {stats['skills']}"
    )
