"""验证备份配置：设置页表单 / 保存同步任务 / 立即备份 / 下载 / 删除 / 保留份数 / 通知渠道。"""
import pathlib
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


def sql(q, args=None):
    c = pymysql.connect(host="192.168.100.150", port=23306, user="ai_bot",
                        password="e484PP6GRwzZGFkX", database="ai_bot")
    cur = c.cursor()
    cur.execute(q, args or ())
    rows = cur.fetchall()
    c.commit()
    c.close()
    return rows


BACKUP_DIR = pathlib.Path("data/backups")
TEST_PREFIX = None  # 本轮备份文件名前缀（UTC 时分），用于清理


def make_backup():
    r = s.post(BASE + "/settings/api/backup-now", json={})
    return r


for _ in range(30):
    try:
        if s.get(BASE + "/login", timeout=2).status_code == 200:
            break
    except requests.RequestException:
        time.sleep(1)

r = s.get(BASE + "/login")
token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.text).group(1)
s.post(BASE + "/login", data={"csrf_token": token, "username": "admin", "password": "admin123"})

# 1. 设置页含备份设置 Tab 与表单
r = s.get(BASE + "/settings")
check("设置页含备份 Tab", "备份设置" in r.text)
check("表单含时间/份数/渠道", 'name="backup_time"' in r.text and 'name="backup_keep"' in r.text
      and 'name="backup_channel"' in r.text)
check("含立即备份与最近备份", "立即备份" in r.text and "最近备份" in r.text)

# 2. 保存配置 → settings 表 + 内置任务 cron/开关 同步
r = s.post(BASE + "/settings/backup", data={
    "csrf_token": token, "backup_enabled": "1", "backup_time": "04:00",
    "backup_keep": "10", "backup_channel": "serverchan",
}, allow_redirects=False)
check("保存备份配置 302", r.status_code == 302)
rows = dict(sql("SELECT `key`, value FROM settings WHERE `key` IN ('backup_time','backup_keep','backup_channel','backup_enabled')"))
check("settings 已写入", rows.get("backup_time") == '"04:00"' and rows.get("backup_keep") == "10"
      and rows.get("backup_channel") == '"serverchan"', str(rows))
rows = sql("SELECT cron, enabled FROM scheduled_jobs WHERE job_key='data_backup'")
check("内置任务 cron 同步 0 4", rows and rows[0][0] == "0 4 * * *" and rows[0][1] == 1, str(rows))

# 3. 立即备份 ×2（文件名含微秒应唯一）
names = []
for _ in range(2):
    r = make_backup()
    check("立即备份成功", r.json().get("ok") is True, r.text[:120])
    names.append(r.json().get("data", {}).get("file"))
check("生成 2 个唯一备份文件", len(set(names)) == 2, str(names))
TEST_PREFIX = names[0][:19]  # backup-YYYYMMDD-HHMM 前缀

# 4. 下载备份（存在文件 → 200 附件）
r = s.get(BASE + f"/settings/backups/download/{names[0]}", allow_redirects=False)
check("下载备份 200 + 附件", r.status_code == 200
      and "attachment" in r.headers.get("Content-Disposition", ""), f"status={r.status_code}")

# 5. 路径穿越防护（不得返回文件，302 回设置页）
for evil in ["..%2f..%2f.env", "....//....//.env", "..\\..\\app\\config.py"]:
    r = s.get(BASE + f"/settings/backups/download/{evil}", allow_redirects=False)
    if r.status_code != 302:
        check(f"路径穿越被拦截（{evil}）", False, f"status={r.status_code}")
        break
else:
    check("路径穿越被拦截", True)

# 6. 删除备份 → 页面表格不再显示
r = s.post(BASE + f"/settings/backups/delete/{names[0]}",
           data={"csrf_token": token}, allow_redirects=False)
check("删除备份 302", r.status_code == 302)
r = s.get(BASE + "/settings")
check("删除后表格不再显示", f"<code>{names[0]}</code>" not in r.text)

# 7. 保留份数：keep=1 后再备份 → 本批只剩 1 份
s.post(BASE + "/settings/backup", data={
    "csrf_token": token, "backup_enabled": "1", "backup_time": "04:00",
    "backup_keep": "1", "backup_channel": "serverchan",
})
r = make_backup()
check("保留 1 份后再备份", r.json().get("ok") is True)
left = [p.name for p in BACKUP_DIR.glob("backup-*.json")]
check("目录只剩 1 份", len(left) == 1, str(left))

# 8. 备份完成通知走配置渠道（serverchan 未配置 → 记录 failed 但渠道正确）
rows = sql("SELECT id FROM scheduled_jobs WHERE job_key='data_backup'")
r = s.post(BASE + f"/jobs/run/{rows[0][0]}", data={"csrf_token": token}, allow_redirects=False)
check("手动执行备份任务", r.status_code == 302)
rows = sql("SELECT channel, status FROM notifications ORDER BY id DESC LIMIT 1")
check("通知渠道为 serverchan", rows and rows[0][0] == "serverchan" and rows[0][1] == "failed",
      str(rows))

# 清理：恢复默认配置 + 删除本轮测试备份
s.post(BASE + "/settings/backup", data={
    "csrf_token": token, "backup_enabled": "1", "backup_time": "03:00",
    "backup_keep": "7", "backup_channel": "inapp",
})
sql("DELETE FROM notifications")
for p in BACKUP_DIR.glob("backup-*.json"):
    p.unlink(missing_ok=True)
print("  (默认配置已恢复，测试备份已清理)")

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
sys.exit(1 if FAILED else 0)
