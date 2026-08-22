"""对话人设：读取/写入 settings、注入系统提示词、HTTP 保存与恢复默认。"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask_login import login_user

from run import app
from app.ai.prompts import (
    DEFAULT_PERSONA_NAME, build_system_prompt, load_persona, persona_name,
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

        p = load_persona(user)
        check("默认名字是灵犀", p["name"] == DEFAULT_PERSONA_NAME)
        check("默认预设 default", p["preset"] == "default")
        prompt = build_system_prompt(user)
        check("默认提示词含灵犀", "你是灵犀（Lingxi）" in prompt, prompt[:80])
        check("默认提示词含工具规则", "先调用工具查询" in prompt)
        check("persona_name 默认", persona_name(user) == "灵犀")

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

        r = client.post("/settings/persona", data={
            "csrf_token": token,
            "reset_persona": "1",
        }, follow_redirects=False)
        check("恢复默认 302", r.status_code == 302)
        p = load_persona(user)
        check("恢复后回到灵犀", p["name"] == DEFAULT_PERSONA_NAME and p["preset"] == "default"
              and p["extra"] == "" and p["address"] == "", str(p))

    r = client.get("/chat/")
    check("对话页展示人设名", r.status_code == 200 and "开始和 灵犀 对话吧" in r.text, str(r.status_code))
    check("对话页有人设入口", "#persona" in r.text)

print(f"\n通过 {len(PASSED)}  失败 {len(FAILED)}")
if FAILED:
    print("失败项：", ", ".join(FAILED))
    sys.exit(1)
