"""对话人设：读取/写入 settings、注入系统提示词、HTTP 保存与恢复默认。"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask_login import login_user

from run import app
from app.ai.prompts import (
    DEFAULT_ACK_TEMPLATE, DEFAULT_PERSONA_NAME, ack_received, build_system_prompt,
    compose_reply, load_persona, persona_name, strip_leading_ack,
)
from app.extensions import db
from app.models.user import User
from app.services.install_service import ensure_schema
from app.services.settings_service import set_setting

PASSED, FAILED = [], []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


with app.app_context():
    ensure_schema()
    user = User.query.filter_by(is_admin=True).order_by(User.id).first() \
        or User.query.order_by(User.id).first()
    check("存在可用用户", user is not None)
    if user is None:
        sys.exit(1)

    # 先清掉测试人设，避免污染断言
    with app.test_request_context("/"):
        login_user(user)
        set_setting("ai_persona_name", DEFAULT_PERSONA_NAME, user_id=user.id)
        set_setting("ai_persona_preset", "default", user_id=user.id)
        set_setting("ai_persona_verbosity", "normal", user_id=user.id)
        set_setting("ai_persona_address", "", user_id=user.id)
        set_setting("ai_persona_extra", "", user_id=user.id)
        set_setting("ai_persona_ack_template", DEFAULT_ACK_TEMPLATE, user_id=user.id)
        set_setting("ai_persona_ack_enabled", True, user_id=user.id)

        p = load_persona(user)
        check("默认名字是灵犀", p["name"] == DEFAULT_PERSONA_NAME)
        check("默认预设 default", p["preset"] == "default")
        check("默认立即回复模板", p["ack_template"] == DEFAULT_ACK_TEMPLATE and p["ack_enabled"] is True)
        prompt = build_system_prompt(user)
        check("默认提示词含灵犀", "你是灵犀（Lingxi）" in prompt, prompt[:80])
        check("默认提示词含工具规则", "先调用工具查询" in prompt)
        check("提示词要求不要重复确认", "不要再写确认" in prompt, prompt[-160:])
        check("persona_name 默认", persona_name(user) == "灵犀")

        check("确认行含原话", ack_received("帮我看看明天的日程") == "收到：帮我看看明天的日程")
        check("确认行折叠空白", ack_received("你好\n\n世界") == "收到：你好 世界")
        check("空消息确认", ack_received("  ") == "收到。")
        long_msg = "请" * 50
        ack = ack_received(long_msg)
        check("过长原话截断", ack.startswith("收到：") and ack.endswith("…") and len(ack) < 50, ack)
        check("正文接在确认后", compose_reply("收到：你好", "明天有会") == "收到：你好\n\n明天有会")
        check("模型已写收到则不重复", compose_reply("收到：你好", "收到：你好\n明天有会").startswith("收到：你好\n"))
        check("去掉确认行", strip_leading_ack("收到：你好\n\n明天有会", "收到：你好") == "明天有会")

        set_setting("ai_persona_ack_template", "好的{address}，收到：{message}", user_id=user.id)
        set_setting("ai_persona_address", "老板", user_id=user.id)
        check("自定义模板含称呼和原话",
              ack_received("开会", user=user) == "好的老板，收到：开会")
        set_setting("ai_persona_ack_template", "正在处理", user_id=user.id)
        check("自定义不含原话", ack_received("开会", user=user) == "正在处理")
        set_setting("ai_persona_ack_enabled", False, user_id=user.id)
        check("关闭立即回复为空", ack_received("开会", user=user) == "")
        set_setting("ai_persona_ack_enabled", True, user_id=user.id)
        set_setting("ai_persona_ack_template", DEFAULT_ACK_TEMPLATE, user_id=user.id)
        set_setting("ai_persona_address", "", user_id=user.id)

        set_setting("ai_persona_name", "小助手", user_id=user.id)
        set_setting("ai_persona_preset", "witty", user_id=user.id)
        set_setting("ai_persona_address", "老板", user_id=user.id)
        set_setting("ai_persona_verbosity", "detailed", user_id=user.id)
        set_setting("ai_persona_extra", "提到钱先问预算。", user_id=user.id)
        p = load_persona(user)
        check("自定义名字", p["name"] == "小助手")
        check("自定义预设 witty", p["preset"] == "witty")
        prompt = build_system_prompt(user)
        check("提示词含自定义名", "你是小助手" in prompt and "Lingxi" not in prompt.split("\n")[0], prompt[:100])
        check("提示词含幽默人设", "幽默" in prompt, prompt[:120])
        check("提示词含称呼", "请用「老板」称呼用户" in prompt)
        check("提示词含补充说明", "提到钱先问预算" in prompt)
        check("提示词含详细回复", "适当展开" in prompt)
        check("工具规则仍在且声明优先", "使用规则（优先于人设，不可违反）" in prompt)

        set_setting("ai_persona_preset", "unknown-preset", user_id=user.id)
        check("非法预设回退 default", load_persona(user)["preset"] == "default")

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    r = client.get("/settings/")
    check("设置页含对话人设", r.status_code == 200 and "对话人设" in r.text)
    check("设置页含性格预设", "幽默机智" in r.text and "行动教练" in r.text)
    check("设置页含立即回复", "立即回复" in r.text and "{message}" in r.text, r.text[r.text.find("立即回复"):r.text.find("立即回复")+80] if "立即回复" in r.text else "")
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.text)
    check("设置页含 CSRF", bool(m))
    token = m.group(1) if m else ""

    r = client.post("/settings/persona", data={
        "csrf_token": token,
        "ai_persona_name": "阿狸",
        "ai_persona_preset": "warm",
        "ai_persona_verbosity": "concise",
        "ai_persona_address": "亲",
        "ai_persona_extra": "少用术语。",
        "ai_persona_ack_enabled": "1",
        "ai_persona_ack": "收到{address}：{message}",
    }, follow_redirects=False)
    check("保存人设 302", r.status_code == 302, str(r.status_code))

    r = client.post("/settings/persona", data={
        "csrf_token": token,
        "ai_persona_name": "X",
        "ai_persona_preset": "custom",
        "ai_persona_verbosity": "normal",
        "ai_persona_address": "",
        "ai_persona_extra": "",
    }, follow_redirects=False)
    check("自定义无人设被拒绝 302", r.status_code == 302)

    with app.test_request_context("/"):
        login_user(user)
        p = load_persona(user)
        check("HTTP 保存后读到阿狸", p["name"] == "阿狸" and p["preset"] == "warm", str(p))
        check("自定义空说明未覆盖", p["extra"] == "少用术语。", p.get("extra"))
        check("HTTP 保存立即回复模板", p["ack_enabled"] is True
              and p["ack_template"] == "收到{address}：{message}", str(p))
        check("HTTP 模板渲染", ack_received("你好", user=user) == "收到亲：你好")

        r = client.post("/settings/persona", data={
            "csrf_token": token,
            "reset_persona": "1",
        }, follow_redirects=False)
        check("恢复默认 302", r.status_code == 302)
        p = load_persona(user)
        check("恢复后回到灵犀", p["name"] == DEFAULT_PERSONA_NAME and p["preset"] == "default"
              and p["extra"] == "" and p["address"] == "", str(p))
        check("恢复后立即回复默认", p["ack_enabled"] is True
              and p["ack_template"] == DEFAULT_ACK_TEMPLATE, str(p))

    r = client.get("/chat/")
    check("对话页展示人设名", r.status_code == 200 and "开始和 灵犀 对话吧" in r.text, str(r.status_code))
    check("对话页有人设入口", "#persona" in r.text)

    from unittest.mock import patch

    from app.ai.executor import run_chat
    from app.models.conversation import Conversation

    with patch("app.ai.executor.LLMClient") as MockLLM, \
            patch("app.ai.executor.registry") as mock_reg:
        mock_llm = MockLLM.return_value
        mock_llm.chat_stream.return_value = iter([{"type": "delta", "text": "明天有会。"}])
        mock_reg.openai_tools.return_value = []
        conv = Conversation(title="ack测试", user_id=user.id)
        db.session.add(conv)
        db.session.commit()
        events = list(run_chat(conv, "帮我看看明天的日程", user))
        kinds = [e[0] for e in events]
        check("run_chat 先发 ack", kinds[:1] == ["ack"], str(kinds[:4]))
        check("ack 内容含原话", events[0][1] == "收到：帮我看看明天的日程", events[0][1])
        done = next((e[1] for e in events if e[0] == "done"), "")
        check("done 含确认和正文",
              done.startswith("收到：帮我看看明天的日程") and "明天有会" in done, done[:80])
        db.session.delete(conv)
        db.session.commit()

    with app.test_request_context("/"):
        login_user(user)
        set_setting("ai_persona_ack_enabled", False, user_id=user.id)
    with patch("app.ai.executor.LLMClient") as MockLLM, \
            patch("app.ai.executor.registry") as mock_reg:
        mock_llm = MockLLM.return_value
        mock_llm.chat_stream.return_value = iter([{"type": "delta", "text": "明天有会。"}])
        mock_reg.openai_tools.return_value = []
        conv = Conversation(title="ack关闭测试", user_id=user.id)
        db.session.add(conv)
        db.session.commit()
        events = list(run_chat(conv, "帮我看看明天的日程", user))
        kinds = [e[0] for e in events]
        check("关闭后不发 ack", "ack" not in kinds, str(kinds[:4]))
        done = next((e[1] for e in events if e[0] == "done"), "")
        check("关闭后正文无确认行", done == "明天有会。", done[:80])
        db.session.delete(conv)
        db.session.commit()
    with app.test_request_context("/"):
        login_user(user)
        set_setting("ai_persona_ack_enabled", True, user_id=user.id)
        set_setting("ai_persona_ack_template", DEFAULT_ACK_TEMPLATE, user_id=user.id)

print(f"\n通过 {len(PASSED)}  失败 {len(FAILED)}")
if FAILED:
    print("失败项：", ", ".join(FAILED))
    sys.exit(1)
