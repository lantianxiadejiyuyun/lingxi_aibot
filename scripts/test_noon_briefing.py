"""验证午间简报：设置页字段 / 保存时间同步任务 / 手动执行产生通知。"""
import re
import sys
import time

import pymysql
import requests

BASE = "http://127.0.0.1:5000"
s = requests.Session()
PASSED, FAILED = [], []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


for _ in range(30):
    try:
        if s.get(BASE + "/login", timeout=2).status_code == 200:
            break
    except requests.RequestException:
        time.sleep(1)

r = s.get(BASE + "/login")
token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.text).group(1)
s.post(BASE + "/login", data={"csrf_token": token, "username": "admin", "password": "admin123"})

# 1. 设置页含午间简报字段（默认 12:00）
r = s.get(BASE + "/settings")
check("设置页含午间简报输入", 'id="briefing_time_noon"' in r.text and 'value="12:00"' in r.text)
check("设置页含三个简报字段", r.text.count('type="time"') == 3)

# 2. 保存 14:30 → 定时任务 cron 同步（回归：分/时不能写反）
r = s.post(BASE + "/settings/briefing", data={
    "csrf_token": token,
    "briefing_time_morning": "07:00",
    "briefing_time_noon": "14:30",
    "briefing_time_evening": "21:00",
}, allow_redirects=False)
check("保存简报设置 302", r.status_code == 302)
c = pymysql.connect(host="192.168.100.150", port=23306, user="ai_bot",
                    password="e484PP6GRwzZGFkX", database="ai_bot")
cur = c.cursor()
cur.execute("SELECT job_key, cron FROM scheduled_jobs WHERE job_key IN ('morning_briefing','noon_briefing','evening_review') ORDER BY job_key")
crons = dict(cur.fetchall())
c.close()
check("午间 14:30 → cron 30 14", crons.get("noon_briefing") == "30 14 * * *", str(crons))
check("早安 07:00 → cron 0 7", crons.get("morning_briefing") == "0 7 * * *", str(crons))
check("晚间 21:00 → cron 0 21", crons.get("evening_review") == "0 21 * * *", str(crons))

# 3. 定时任务页出现午间简报行（cron 30 14）
r = s.get(BASE + "/jobs")
check("任务页含午间简报", "午间简报" in r.text)
m = re.search(r'<tr[^>]*>[\s\S]{0,300}?午间简报[\s\S]{0,200}?value="(\d+)"', r.text)
check("午间简报行存在", bool(m))

# 4. 手动执行午间简报 → 站内通知（任务 id 直接查库，避免页面顺序歧义）
noon_run = None
c = pymysql.connect(host="192.168.100.150", port=23306, user="ai_bot",
                    password="e484PP6GRwzZGFkX", database="ai_bot")
cur = c.cursor()
cur.execute("SELECT id FROM scheduled_jobs WHERE job_key='noon_briefing'")
row = cur.fetchone()
c.close()
noon_run = str(row[0]) if row else None
check("找到午间简报任务 id", bool(noon_run))
if noon_run:
    r = s.post(BASE + f"/jobs/run/{noon_run}", data={"csrf_token": token}, allow_redirects=False)
    check("手动执行午间简报", r.status_code == 302)
    for _ in range(10):
        r = s.get(BASE + "/notifications")
        if "午间简报" in r.text:
            break
        time.sleep(1)
    check("午间简报产生通知", "午间简报" in r.text)

# 5. 恢复默认 12:00
s.post(BASE + "/settings/briefing", data={
    "csrf_token": token,
    "briefing_time_morning": "07:00",
    "briefing_time_noon": "12:00",
    "briefing_time_evening": "21:00",
})
check("恢复午间默认 12:00", "已恢复")

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
sys.exit(1 if FAILED else 0)
