"""记忆（上下文梳理）测试：长对话压缩 + 长期记忆抽取 + 上下文注入 + 手动触发 + 页面/删除。"""
import os
import re
import sys

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


s = requests.Session()
token = test_common.login(s, BASE)
check("登录成功", bool(token))
check("记忆页 200", s.get(BASE + "/memory").status_code == 200)

# ---- 进程内：播种一个 14 条消息的长对话 ----
from run import app
from app.extensions import db
from app.models.conversation import Conversation, Message
from app.models.memory import Memory
from app.models.user import User
from app.services import memory_service
from app.ai.memory import build_messages
from app.utils.timeutil import utcnow

facts = ["我叫小明", "我喜欢喝黑咖啡", "我住在杭州西湖区", "我是软件工程师", "我常用 Python"]
conv_id = None
with app.app_context():
    # 幂等清理：删掉上次残留的测试会话与自动记忆
    for c in Conversation.query.filter(Conversation.title == "记忆测试会话").all():
        db.session.delete(c)
    Memory.query.filter(Memory.source == "auto").update({"deleted_at": utcnow()},
                                                        synchronize_session=False)
    db.session.commit()

    user = User.query.first()
    conv = Conversation(title="记忆测试会话")
    db.session.add(conv)
    db.session.flush()
    conv_id = conv.id
    for i in range(7):
        db.session.add(Message(role="user", content=f"记住这条：{facts[i % len(facts)]}",
                               conversation_id=conv.id))
        db.session.add(Message(role="assistant", content=f"好的，已记住。", conversation_id=conv.id))
    db.session.commit()
    check("播种长对话", True)

# ---- 手动触发梳理（HTTP，服务器进程内执行，消耗 LLM 调用）----
r = s.post(BASE + "/memory/api/consolidate", json={})
ok = r.json().get("ok") is True
d = (r.json().get("data") or {}) if ok else {}
check("手动触发梳理", ok, str(r.json()))
check("压缩了会话", int(d.get("summarized") or 0) >= 1, str(d))
check("抽取了记忆", int(d.get("memories") or 0) >= 1, str(d))

# ---- 验证摘要与注入 ----
with app.app_context():
    user = User.query.first()  # 重新获取，避免跨上下文 detached
    conv = db.session.get(Conversation, conv_id)
    check("会话摘要已写入", bool(conv.summary), (conv.summary or "")[:80])
    mem_ctx = memory_service.build_memory_context()
    check("长期记忆上下文非空", bool(mem_ctx))
    msgs = build_messages(conv, user)
    check("注入长期记忆", any("长期记忆" in m.get("content", "") for m in msgs))
    check("注入会话摘要", any("本会话历史摘要" in m.get("content", "") for m in msgs))
    user_msgs = [m for m in msgs if m.get("role") in ("user", "assistant")]
    check("有摘要时窗口 ≤ 8", len(user_msgs) <= 8, f"len={len(user_msgs)}")

# ---- 记忆页展示 + 删除 ----
r = s.get(BASE + "/memory")
check("记忆页含自动记忆", "自动" in r.text)
ids = sorted(set(re.findall(r'data-delete-memory="(\d+)"', r.text)))
if ids:
    r = s.post(BASE + "/memory/api/delete", json={"memory_id": ids[0]})
    check("HTTP 删除记忆", r.json().get("ok") is True, str(r.json()))

# ---- 清理 ----
with app.app_context():
    conv = db.session.get(Conversation, conv_id)
    if conv is not None:
        db.session.delete(conv)
    # 软删除本次自动抽取的记忆（功能刚上线，无既有自动记忆，安全）
    Memory.query.filter(Memory.source == "auto").update({"deleted_at": utcnow()},
                                                        synchronize_session=False)
    db.session.commit()
print("  (测试会话与自动记忆已清理)")

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：", FAILED)
    sys.exit(1)
