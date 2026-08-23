"""飞书应用机器人验证：URL 握手 / 消息接收处理 / 去重 / 加密事件 / 未配置降级。"""
import base64
import json
import sys
import time

import pymysql
import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

BASE = "http://127.0.0.1:5000"
PASSED, FAILED = [], []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


def sql(q, args=None):
    c = pymysql.connect(host="192.168.100.150", port=23306, user="ai_bot",
                        password="e484PP6GRwzZGFkX", database="ai_bot")
    cur = c.cursor()
    cur.execute(q, args or ())
    rows = cur.fetchall()
    c.commit()
    c.close()
    return rows


# 配置验证令牌 + 加密密钥（写入 settings 表）
ENCRYPT_KEY = "0123456789abcdef0123456789abcdef"  # 32 字节 AES-256 密钥
for k, v in [("feishu_event_token", "vt-test-123"), ("feishu_event_encrypt_key", ENCRYPT_KEY)]:
    sql("INSERT INTO settings (`key`, value) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE value = VALUES(value)", (k, json.dumps(v)))

# 1. URL 验证握手（正确 token）
r = requests.post(BASE + "/feishu/event", json={
    "type": "url_verification", "token": "vt-test-123", "challenge": "challenge-abc",
})
check("URL 验证返回 challenge", r.status_code == 200 and r.json().get("challenge") == "challenge-abc",
      r.text[:120])

# 2. URL 验证（错误 token → 403）
r = requests.post(BASE + "/feishu/event", json={
    "type": "url_verification", "token": "wrong", "challenge": "x",
})
check("错误 token 返回 403", r.status_code == 403, f"status={r.status_code}")

# 3. 文本消息事件（v2 schema）→ 200 应答 + 后台创建会话
chat_id = "oc_test_chat_001"
msg_id = "om_test_msg_001"
r = requests.post(BASE + "/feishu/event", json={
    "schema": "2.0",
    "header": {"event_id": "evt1", "event_type": "im.message.receive_v1", "token": "vt-test-123"},
    "event": {
        "sender": {"sender_id": {"open_id": "ou_test"}},
        "message": {"message_id": msg_id, "chat_id": chat_id,
                    "chat_type": "p2p", "message_type": "text",
                    "content": json.dumps({"text": "你好，帮我看看今天的日程"})},
        "app_id": "cli_test",
    },
})
check("消息事件应答 code 0", r.status_code == 200 and r.json().get("code") == 0, r.text[:120])
# 后台线程异步处理：轮询 feishu_chat_map 映射落库（最多 10 秒）
mapping = {}
for _ in range(20):
    time.sleep(0.5)
    rows = sql("SELECT value FROM settings WHERE `key` = 'feishu_chat_map'")
    if rows and rows[0][0]:
        mapping = json.loads(rows[0][0])
    if mapping.get(chat_id):
        break
check("收到消息自动建会话并映射", bool(mapping.get(chat_id)), str(mapping))
conv_id = mapping.get(chat_id)

# 4. 同一 message_id 去重（再次发送不应新建会话）
before = conv_id
r = requests.post(BASE + "/feishu/event", json={
    "schema": "2.0",
    "header": {"event_id": "evt2", "event_type": "im.message.receive_v1", "token": "vt-test-123"},
    "event": {"message": {"message_id": msg_id, "chat_id": chat_id,
                          "message_type": "text",
                          "content": json.dumps({"text": "重复消息"})}},
})
time.sleep(1.5)
rows = sql("SELECT value FROM settings WHERE `key` = 'feishu_chat_map'")
mapping2 = json.loads(rows[0][0]) if rows and rows[0][0] else {}
check("重复 message_id 去重", mapping2.get(chat_id) == before, f"{before} → {mapping2}")

# 5. 加密事件（AES-256-CBC 模拟飞书加密）
def feishu_encrypt(plain: str, key: str) -> str:
    iv = b"1234567890abcdef"
    padder = PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plain.encode()) + padder.finalize()
    cipher = Cipher(algorithms.AES(key.encode()), modes.CBC(iv))
    enc = cipher.encryptor()
    return base64.b64encode(iv + enc.update(padded) + enc.finalize()).decode()

encrypted = feishu_encrypt(json.dumps({
    "schema": "2.0",
    "header": {"event_id": "evt3", "event_type": "im.message.receive_v1", "token": "vt-test-123"},
    "event": {"message": {"message_id": "om_test_msg_002", "chat_id": "oc_test_chat_002",
                          "message_type": "text",
                          "content": json.dumps({"text": "加密消息测试"})}},
}), ENCRYPT_KEY)
r = requests.post(BASE + "/feishu/event", json={"encrypt": encrypted})
check("加密事件解密并应答", r.status_code == 200 and r.json().get("code") == 0, r.text[:120])
mapping3 = {}
for _ in range(20):
    time.sleep(0.5)
    rows = sql("SELECT value FROM settings WHERE `key` = 'feishu_chat_map'")
    if rows and rows[0][0]:
        mapping3 = json.loads(rows[0][0])
    if mapping3.get("oc_test_chat_002"):
        break
check("加密事件同样建会话", bool(mapping3.get("oc_test_chat_002")), str(mapping3))

# 6. 非文本消息忽略
r = requests.post(BASE + "/feishu/event", json={
    "schema": "2.0",
    "header": {"event_id": "evt4", "event_type": "im.message.receive_v1", "token": "vt-test-123"},
    "event": {"message": {"message_id": "om_test_msg_003", "chat_id": "oc_test_chat_003",
                          "message_type": "image", "content": json.dumps({"image_key": "x"})}},
})
check("非文本消息忽略", r.status_code == 200 and r.json().get("code") == 0)

# 7. 通知渠道 feishu_app 未配置 → 记录失败
s = requests.Session()
r = s.get(BASE + "/login")
import re
token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.text).group(1)
s.post(BASE + "/login", data={"csrf_token": token, "username": "admin", "password": "admin123"})
s.headers["X-CSRFToken"] = token
r = s.post(BASE + "/settings/api/test-channel", json={"channel": "feishu_app"})
d = r.json()
check("feishu_app 未配置返回失败", d.get("ok") is True and d.get("data", {}).get("status") == "failed",
      str(d))
check("失败原因明确", "未配置" in (d.get("data", {}).get("error") or ""), str(d))
r = s.get(BASE + "/settings")
check("设置页含飞书机器人 Tab", "飞书机器人" in r.text and "im.message.receive_v1" in r.text)
r = s.post(BASE + "/settings/api/test-feishu-app", json={})
check("测试接口未配置提示", "尚未配置 App ID" in (r.json().get("error") or ""), r.text[:120])

# 清理测试数据（等后台线程处理完再删，避免与写入并发造成死锁）
time.sleep(3)
sql("DELETE FROM messages WHERE conversation_id IN "
    "(SELECT id FROM conversations WHERE title IN ('你好，帮我看看今天的日程','加密消息测试'))")
sql("DELETE FROM conversations WHERE title IN ('你好，帮我看看今天的日程','加密消息测试')")
sql("DELETE FROM settings WHERE `key` IN ('feishu_event_token','feishu_event_encrypt_key','feishu_chat_map')")
sql("DELETE FROM notifications WHERE title LIKE '%%测试%%'")

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
sys.exit(1 if FAILED else 0)
