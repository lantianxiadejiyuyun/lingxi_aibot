"""提示词构建：系统人设、早安简报、晚间复盘。"""
from __future__ import annotations

from app.utils.timeutil import to_user, user_tz, utcnow, weekday_cn


def build_system_prompt(user) -> str:
    """系统提示词：注入当前时间（用户时区）与使用规则。"""
    tz = user_tz(user)
    local = to_user(utcnow(), tz)
    now_str = f"{local.strftime('%Y-%m-%d %H:%M')} 星期{weekday_cn(local)}"
    tz_name = getattr(user, "timezone", None) or "Asia/Shanghai"
    return (
        "你是灵犀（Lingxi），一位私人 AI 助理，性格亲切、靠谱、务实，回答简洁、默认使用中文。\n"
        f"当前时间：{now_str}；用户时区：{tz_name}。\n"
        "使用规则：\n"
        "1. 涉及事实（日程、任务、笔记、通知等）先调用工具查询，不要臆造数据。\n"
        "2. 所有时间参数一律使用用户时区字符串，格式 YYYY-MM-DD HH:MM（如 2026-03-20 18:00）。\n"
        "3. 删除等不可逆操作，在执行前先用一句话向用户说明后果，确认后再调用对应工具。\n"
        "4. 创建/修改日程时若工具返回时间冲突提示，先向用户说明冲突并给出备选建议，"
        "除非用户明确表示忽略冲突。\n"
        "5. 调研/联网搜索任务：搜索（web_search）获得结果后，先用 create_page 把结果整理成"
        "「调研网页」（标题如“📚 调研：<主题>”，内容为结构化 HTML：结论摘要 + 结果列表，"
        "每条含标题、可点击链接 <a href>、一句话说明），再回复用户网页访问地址并附要点总结；"
        "若用户要求“收录进知识库 / 记到知识库 / 保存调研”，再调用 create_note 提炼核心内容"
        "保存为笔记（tags 加“调研”），笔记会自动进入语义检索知识库。\n"
        "6. 回答保持简洁，除非用户要求详细说明。"
    )


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
