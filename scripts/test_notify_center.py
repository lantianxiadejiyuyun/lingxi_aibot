"""通知统一整合测试：notify_for 三级路由（任务覆盖 > 场景设置 > 全局默认）+ 设置页场景渠道保存。"""
import os
import re
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


# ============ HTTP：设置页场景渠道保存 ============
s = requests.Session()
token = __import__("test_common").login(s, BASE)
check("登录成功", bool(token))
r = s.get(BASE + "/settings")
check("设置页含场景渠道", "各场景推送渠道" in r.text and 'name="scene_briefing"' in r.text)

# 保存：briefing 场景 → feishu，reminder → serverchan
r = s.post(BASE + "/settings/channels", data={
    "csrf_token": token, "sc_key": "", "feishu_webhook_url": "", "feishu_secret": "",
    "default_channels": "inapp", "scene_briefing": "feishu", "scene_report": "",
    "scene_backup": "", "scene_cleanup": "", "scene_reminder": "serverchan"},
    allow_redirects=False)
check("保存场景渠道", r.status_code == 302, f"实际 {r.status_code}")

# ============ 进程内：notify_for 三级路由 ============
from run import app
from app.extensions import db
from app.services import notify_service
from app.models.notification import Notification

with app.app_context():
    from app.services.settings_service import get_setting

    # 1. 场景设置生效（briefing→feishu）
    recs = notify_service.notify_for("briefing", "测试场景", "正文")
    chans = {rec.channel for rec in recs}
    check("场景渠道生效(briefing→feishu)", "feishu" in chans, str(chans))
    # 2. 未配置场景跟随全局默认（report 未设置 → inapp）
    recs = notify_service.notify_for("report", "测试默认", "正文")
    check("未配置场景跟随全局", all(r.channel == "inapp" for r in recs), str({r.channel for r in recs}))
    # 3. explicit（任务页覆盖）优先
    recs = notify_service.notify_for("briefing", "覆盖测试", "正文", explicit="serverchan")
    check("explicit 覆盖场景", all(r.channel == "serverchan" for r in recs),
          str({r.channel for r in recs}))
    # 4. 旧键兼容：backup 场景未设置时读旧 backup_channel
    from app.services.settings_service import set_setting
    set_setting("notify_backup_channels", [])
    set_setting("backup_channel", "inapp")
    recs = notify_service.notify_for("backup", "备份测试", "正文")
    check("backup 旧键兼容", all(r.channel == "inapp" for r in recs))

    # 5. 动作级：数据备份走 backup 场景渠道
    from app.services.backup_service import run_backup
    before = Notification.query.count()
    run_backup({})
    after = Notification.query.count()
    latest = Notification.query.order_by(Notification.id.desc()).first()
    check("备份动作产生通知", after > before and latest is not None
          and latest.title == "🗄️ 数据备份完成" and latest.channel == "inapp",
          f"title={latest.title if latest else None} channel={latest.channel if latest else None}")

    # 清理测试产生的通知
    Notification.query.filter(Notification.title.in_(
        ["测试场景", "测试默认", "覆盖测试", "备份测试"])).delete(synchronize_session=False)
    db.commit()

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：", FAILED)
    sys.exit(1)
