"""端到端冒烟测试：登录、各页面、CRUD API、通知、定时任务、AI 会话（无 Key 降级）、调度器提醒。"""
import re
import sys
import time
from datetime import datetime, timedelta

import requests
import test_common

BASE = "http://127.0.0.1:5000"
s = requests.Session()
PASSED, FAILED = [], []

NOW = datetime.now()
T_MEETING = (NOW + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
T_MEETING_END = (NOW + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
T_REMIND = (NOW + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M")
T_DUE = (NOW + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


# 等待服务器就绪
for _ in range(30):
    try:
        if s.get(BASE + "/login", timeout=2).status_code == 200:
            break
    except requests.RequestException:
        time.sleep(1)
else:
    print("服务器未就绪"); sys.exit(1)

# ---- 登录 ----
token = test_common.login(s, BASE)
check("登录成功", bool(token))
s.get(BASE + "/")

# ---- 各页面 ----
for path in ["/", "/calendar", "/tasks", "/notes", "/chat", "/jobs", "/notifications", "/settings"]:
    r = s.get(BASE + path)
    check(f"页面 {path}", r.status_code == 200, f"status={r.status_code}")

# ---- 日历 CRUD ----
r = s.post(BASE + "/calendar/api/create", json={
    "title": "冒烟测试会议", "start": T_MEETING, "end": T_MEETING_END,
    "description": "e2e", "repeat": "none", "reminder_minutes": None,
})
check("创建事件", r.json().get("ok") is True, str(r.json()))
event_id = (r.json().get("data") or {}).get("id")
r = s.get(BASE + "/calendar/api/events", params={"year": NOW.year, "month": NOW.month})
evs = (r.json().get("data") or {}).get("events", [])
check("查询事件含新建", any(str(e.get("id")) == str(event_id) for e in evs))
r = s.post(BASE + "/calendar/api/update", json={"event_id": event_id, "title": "冒烟测试会议(改)"})
check("更新事件", r.json().get("ok") is True, str(r.json()))

# 提醒扫描：事件 2 分钟后开始、准时提醒 → 调度器每分钟扫描应命中并产生站内通知
r = s.post(BASE + "/calendar/api/create", json={
    "title": "调度器提醒测试", "start": T_REMIND, "reminder_minutes": 0,
})
rid = (r.json().get("data") or {}).get("id")
check("创建提醒事件", r.json().get("ok") is True)

# ---- 任务 CRUD ----
r = s.post(BASE + "/tasks/api/create", json={"title": "冒烟测试任务", "due": T_DUE,
                                             "priority": 3, "tags": "测试,冒烟"})
check("创建任务", r.json().get("ok") is True, str(r.json()))
task_id = (r.json().get("data") or {}).get("id")
r = s.post(BASE + "/tasks/api/toggle", json={"task_id": task_id})
check("完成任务", r.json().get("ok") is True, str(r.json()))
r = s.post(BASE + "/tasks/api/toggle", json={"task_id": task_id})
check("恢复任务", r.json().get("ok") is True)

# ---- 笔记 CRUD ----
r = s.post(BASE + "/notes/api/create", json={"title": "冒烟笔记", "content": "这是内容 123", "tags": "测试"})
check("创建笔记", r.json().get("ok") is True, str(r.json()))
note_id = (r.json().get("data") or {}).get("id")
r = s.post(BASE + "/notes/api/update", json={"note_id": note_id, "content": "更新后的内容"})
check("更新笔记", r.json().get("ok") is True)

# ---- 通知渠道 ----
r = s.post(BASE + "/settings/api/test-channel", json={"channel": "inapp"})
check("站内测试通知", r.json().get("ok") is True, str(r.json()))
r = s.get(BASE + "/notifications")
check("通知中心有记录", r.status_code == 200 and "测试通知" in r.text)

# ---- 定时任务：创建自定义提醒 ----
r = s.post(BASE + "/jobs/create", data={"csrf_token": token, "name": "冒烟定时提醒",
                                        "cron": "30 9 * * *", "title": "喝水", "body": "该喝水了",
                                        "channels": "inapp"}, allow_redirects=False)
check("创建定时提醒", r.status_code == 302, f"实际 {r.status_code}")
r = s.get(BASE + "/jobs")
check("定时任务页含新任务", "冒烟定时提醒" in r.text)

# ---- 早安简报手动执行（无 LLM Key → 纯文本降级 + 站内通知）----
job_ids = dict(re.findall(r'action="(/jobs/run/(\d+))"', r.text))
run_ok = False
r2 = s.get(BASE + "/jobs")
page = r2.text
m = re.search(r'name="任务"[^>]*|morning|早安', page)
# 找到早安简报对应的 run 链接：按顺序取 run 链接中第 3 个（seed 顺序：提醒扫描/到期扫描/早安简报/晚间复盘/备份）
runs = re.findall(r'/jobs/run/(\d+)', page)
if len(runs) >= 3:
    r = s.post(BASE + "/jobs/run/" + runs[2], data={"csrf_token": token}, allow_redirects=False)
    run_ok = r.status_code == 302
    s.get(BASE + "/")
check("手动执行早安简报", run_ok)
r = s.get(BASE + "/notifications")
check("简报产生站内通知", "早安简报" in r.text, "")

# ---- 调度器：等到提醒时间，再预留一个扫描周期及少量执行余量 ----
hit = False
until_due = max(0, (datetime.strptime(T_REMIND, "%Y-%m-%d %H:%M") - datetime.now()).total_seconds())
deadline = time.monotonic() + until_due + 75
while time.monotonic() < deadline:
    time.sleep(2)
    r = s.get(BASE + "/notifications")
    if "调度器提醒测试" in r.text:
        hit = True
        break
check("调度器自动提醒（≤100s）", hit)

# ---- AI 对话（SSE 事件流；未配置 Key 时降级为 error 事件）----
r = s.post(BASE + "/chat/api/send", json={"message": "你好"})
check("AI 会话返回 done 事件", "event: done" in r.text, r.text[:150].replace("\n", "|"))
if "未配置 API Key" in r.text:
    check("未配置 Key 返回错误提示", "event: error" in r.text)
else:
    check("已配置 Key 产生流式回复", "event: delta" in r.text, r.text[:150].replace("\n", "|"))

# ---- 清理：软删除测试数据 ----
if event_id:
    s.post(BASE + "/calendar/api/delete", json={"event_id": event_id})
if rid:
    s.post(BASE + "/calendar/api/delete", json={"event_id": rid})
if task_id:
    s.post(BASE + "/tasks/api/delete", json={"task_id": task_id})
if note_id:
    s.post(BASE + "/notes/api/delete", json={"note_id": note_id})
# 删除测试创建的定时提醒（避免重复残留；行内只有 form="job-delete-<id>"）
r = s.get(BASE + "/jobs")
for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S):
    if "冒烟定时提醒" in tr:
        m3 = re.search(r'form="job-delete-(\d+)"', tr)
        if m3:
            s.post(BASE + "/jobs/delete/" + m3.group(1),
                   data={"csrf_token": token}, allow_redirects=False)
print("  (测试数据已软删除，定时提醒已清理)")

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：", FAILED)
    sys.exit(1)
