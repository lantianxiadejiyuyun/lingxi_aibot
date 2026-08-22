"""阶段B测试：记忆重要度/过期衰减 + 每周自动整理（归档/合并/坏链/清通知）。"""
import os
import re
import sys
from datetime import timedelta

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


# ============ HTTP：记忆页与任务页 ============
s = requests.Session()
token = test_common.login(s, BASE)
check("登录成功", bool(token))
r = s.get(BASE + "/jobs")
check("任务页含每周整理", "每周整理" in r.text)

# ============ 进程内 ============
from run import app
from app.extensions import db
from app.models.memory import Memory
from app.models.note import Note
from app.models.notification import Notification
from app.models.task import STATUS_OPEN, Task
from app.models.webpage import WebPage
from app.services import maintenance_service, memory_service, note_service, page_service, task_service
from app.utils.timeutil import utcnow

with app.app_context():
    # 幂等预清理：删除上次运行残留的测试数据
    for mm in Memory.query.filter(Memory.content.like("B阶段%")).all():
        memory_service.soft_delete_memory(mm)
    for tt in Task.query.filter(Task.title == "B阶段超期任务",
                                Task.deleted_at.is_(None)).all():
        task_service.soft_delete_task(tt)
    for nn in Note.query.filter(Note.title.like("B阶段%"),
                                Note.deleted_at.is_(None)).all():
        note_service.soft_delete_note(nn)
    for pp in WebPage.query.filter(WebPage.title == "B阶段坏链页",
                                   WebPage.deleted_at.is_(None)).all():
        page_service.soft_delete_page(pp)
    # ---------- 记忆重要度与过期衰减 ----------
    mem1 = memory_service.remember("B阶段重要记忆", importance=5)
    mem2 = memory_service.remember("B阶段过期记忆", importance=2,
                                   expires_at=utcnow() - timedelta(days=1))
    ctx = memory_service.build_memory_context()
    check("重要记忆注入", "B阶段重要记忆" in ctx)
    check("过期记忆被过滤", "B阶段过期记忆" not in ctx)
    n_expired = memory_service.delete_expired()
    check("过期记忆被清理", n_expired >= 1, f"n={n_expired}")
    check("过期记忆已软删", db.session.get(Memory, mem2.id).deleted_at is not None)

    # 记忆页渲染（此时已有记忆，表头应含重要度）
    r = s.get(BASE + "/memory")
    check("记忆页含重要度列", "重要度" in r.text and "★" in r.text)

    # remember 工具解析 expires（request 上下文内）
    with app.test_request_context("/"):
        from flask_login import login_user
        from app.models.user import User

        user = User.query.first()
        login_user(user)
        from app.ai import registry

        r = registry.execute_tool("remember", {
            "content": "B阶段带过期记忆", "importance": 4, "expires": "2030-01-01"})
        check("remember 工具支持重要度/过期", "已记住" in r, r[:60])
        mem3 = Memory.query.filter(Memory.content == "B阶段带过期记忆").first()
        check("重要度已保存", mem3 is not None and mem3.importance == 4 and mem3.expires_at is not None)
        lst = registry.execute_tool("list_memories", {})
        check("list_memories 含重要度", "importance" in lst or "4" in str(lst), str(lst)[:100])

    # ---------- 每周整理 ----------
    # 1) 超期任务归档
    t = task_service.create_task("B阶段超期任务", due_naive=utcnow() - timedelta(days=10))
    n = maintenance_service.cleanup_overdue_tasks()
    check("超期任务归档", n >= 1 and db.session.get(Task, t.id).status == "cancelled", f"n={n}")
    # 2) 重复笔记合并
    n1 = note_service.create_note("B阶段重复笔记", content="第一段")
    n2 = note_service.create_note("B阶段重复笔记", content="第二段")
    nm = maintenance_service.merge_duplicate_notes()
    check("重复笔记合并", nm >= 1, f"n={nm}")
    check("合并后内容完整", "第一段" in note_service.list_notes()[0].content
          and "第二段" in note_service.list_notes()[0].content)
    # 3) 网页坏链体检（内网链接被 SSRF 防护跳过，不误报也不探测内网）
    p = page_service.create_page(
        "B阶段坏链页",
        '<html><body><a href="http://127.0.0.1:1/nope">内网</a>'
        '<a href="http://192.168.0.1/x">内网2</a></body></html>',
        slug=None)
    broken = maintenance_service.check_page_links()
    check("内网链接被 SSRF 跳过", len(broken) == 0, str(broken)[:120])
    check("check_page_links 不抛错", True)
    # 4) 旧通知清理
    old = Notification(channel="inapp", title="旧通知", body="", read=True,
                       created_at=utcnow() - timedelta(days=40))
    db.session.add(old)
    db.session.commit()
    old_id = old.id  # 提交后实例过期，先取出 id 再清理
    nr = maintenance_service.cleanup_old_notifications()
    check("旧通知清理", nr >= 1 and db.session.get(Notification, old_id) is None, f"n={nr}")

    # 汇总执行（验证 run_cleanup 不抛错）
    report = maintenance_service.run_cleanup()
    check("run_cleanup 汇总", isinstance(report, dict) and "tasks_archived" in report, str(report)[:80])

    # ---------- 清理测试数据 ----------
    for mm in Memory.query.filter(Memory.content.like("B阶段%")).all():
        memory_service.soft_delete_memory(mm)
    task_service.soft_delete_task(Task.query.get(t.id))
    for nn in note_service.list_notes():
        if nn.title.startswith("B阶段"):
            note_service.soft_delete_note(nn)
    page_service.soft_delete_page(page_service.get_page(p.id))
print("  (B阶段测试数据已清理)")

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：", FAILED)
    sys.exit(1)
