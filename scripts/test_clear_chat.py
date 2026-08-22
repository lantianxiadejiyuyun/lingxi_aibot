"""清空所有对话测试：创建会话与消息 → clear-all → 会话与消息全部删除。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

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
token = __import__("test_common").login(s, BASE)
check("登录成功", bool(token))

# 记录清空前会话/消息数
from run import app
from app.extensions import db
from app.models.conversation import Conversation, Message

with app.app_context():
    before_conv = Conversation.query.count()
    before_msg = Message.query.count()

# 创建 2 个测试会话（各含 1 条消息）
for i in range(2):
    r = s.post(BASE + "/chat/api/new", json={})
    check(f"创建会话{i}", r.json().get("ok") is True)
    cid = r.json()["data"]["id"]
    r = s.post(BASE + "/chat/api/send", json={"conversation_id": cid, "message": f"清空测试{i}"})
    check(f"会话{i}发送消息", "event: done" in r.text or "event: error" in r.text)

# 执行清空
r = s.post(BASE + "/chat/api/clear-all", json={})
d = r.json()
check("clear-all 返回 ok", d.get("ok") is True, str(d))
check("删除数量正确", int(d.get("data", {}).get("deleted") or 0) >= before_conv + 2,
      f"deleted={d.get('data', {}).get('deleted')} before={before_conv}")

# 验证会话与消息全部删除
r = s.get(BASE + "/chat/api/conversations")
check("会话列表为空", r.json().get("ok") is True and (r.json().get("data") or []) == [],
      str(r.json())[:100])
with app.app_context():
    after_conv = Conversation.query.count()
    after_msg = Message.query.count()
check("DB 会话为 0", after_conv == 0, f"after={after_conv}")
check("DB 消息级联删除为 0", after_msg == 0, f"after={after_msg}")

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：", FAILED)
    sys.exit(1)
