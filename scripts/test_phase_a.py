"""阶段A测试：日程冲突检测 / 周报生成 / 主动早安引导 / 语音接口。"""
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import test_common

BASE = "http://127.0.0.1:5000"
PASSED, FAILED = [], []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


# ============ HTTP：语音接口与设置页 ============
s = requests.Session()
token = test_common.login(s, BASE)
check("登录成功", bool(token))
r = s.get(BASE + "/settings")
check("设置页含语音 tab", "data-tab=\"voice\"" in r.text and "语音" in r.text)
r = s.post(BASE + "/chat/api/tts", json={"text": "你好"})
check("未配置 TTS 返回 400 提示", r.status_code == 400 and "未配置" in (r.json() or {}).get("error", ""),
      f"status={r.status_code} {r.text[:80]}")

# ============ 进程内：冲突检测 / 周报 / 早安引导 ============
from run import app
from app.extensions import db
from app.ai import registry
from app.services import calendar_service
from app.models.user import User
from app.utils.timeutil import get_tz, to_naive_utc

with app.app_context():
    user = User.query.first()
    tz = get_tz(user.timezone)
    d1 = (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%d")
    t1s, t1e = f"{d1} 10:00", f"{d1} 11:00"
    t2s, t2e = f"{d1} 10:30", f"{d1} 11:30"
    _lo = to_naive_utc(datetime.strptime(t1s, "%Y-%m-%d %H:%M").replace(tzinfo=tz))
    _hi = to_naive_utc(datetime.strptime(t2e, "%Y-%m-%d %H:%M").replace(tzinfo=tz))

    # 预清理：删除上次运行残留的冲突测试事件
    for e in calendar_service.list_events(_lo, _hi):
        if e.title.startswith("冲突测试"):
            calendar_service.soft_delete_event(e)

    with app.test_request_context("/"):
        from flask_login import login_user

        login_user(user)

        # --- 冲突检测 ---
        r1 = registry.execute_tool("create_event", {
            "title": "冲突测试A", "start": t1s, "end": t1e})
        check("创建事件A成功", "已创建" in r1, r1[:60])
        r2 = registry.execute_tool("create_event", {
            "title": "冲突测试B", "start": t2s, "end": t2e})
        check("重叠时间被拦截", "冲突" in r2, r2[:80])
        events = calendar_service.list_events(_lo, _hi)
        event_a = next((e for e in events if e.title == "冲突测试A"), None)
        check("冲突事件未创建", all(e.title != "冲突测试B" for e in events))
        r3 = registry.execute_tool("create_event", {
            "title": "冲突测试B", "start": t2s, "end": t2e, "ignore_conflicts": True})
        check("ignore_conflicts 强制创建", "已创建" in r3, r3[:60])
        event_b = next((e for e in calendar_service.list_events(_lo, _hi)
                        if e.title == "冲突测试B"), None)
        # update_event 冲突（把 A 移到 B 的时间，排除自身后仍与 B 冲突）
        r4 = registry.execute_tool("update_event", {
            "event_id": event_a.id, "start": t2s, "end": t2e})
        check("update 冲突被拦截", "冲突" in r4, r4[:80])

    # --- 周报生成（LLM）---
    from app.ai.report import build_report

    content = build_report("weekly", user)
    check("周报生成成功", bool(content) and content.strip().startswith("##"), content[:60])

    # --- 主动早安：运行 morning 动作（LLM），检查引导消息 ---
    from app.ai.briefing import morning
    from app.models.conversation import Conversation, Message

    morning({})
    today = datetime.now(tz).date().isoformat()
    conv = Conversation.query.filter(Conversation.title == f"☀️ 早安简报 {today}").first()
    check("早报会话存在", conv is not None)
    guide = None
    if conv is not None:
        guide = next((msg.content for msg in conv.messages
                      if "早上好！今日安排已整理好" in msg.content), None)
    check("早报会话含引导消息", bool(guide), str(guide)[:50])

    # --- 清理 ---
    for e in calendar_service.list_events(_lo, _hi):
        if e.title.startswith("冲突测试"):
            calendar_service.soft_delete_event(e)
print("  (测试事件已软删除)")

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：", FAILED)
    sys.exit(1)
