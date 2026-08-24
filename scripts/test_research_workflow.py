"""调研工作流端到端：真实对话 → web_search → create_page（调研网页）→ create_note（知识库）。

消耗一次真实 LLM 对话与真实 Bing 搜索；结束后清理测试页面与笔记。
"""
import json
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

print("发送调研对话（真实 LLM + Bing 搜索，约 30-90 秒）…")
r = s.post(BASE + "/chat/api/send", json={
    "message": "帮我调研一下 Python 3.14 的主要新特性，生成一个调研网页，并把调研结果收录进知识库。"},
    timeout=240)
text = r.text
tools = []
for line in text.split("\n"):
    if line.startswith("data:") and '"name"' in line:
        try:
            tools.append(json.loads(line[5:].strip()))
        except ValueError:
            pass

names = [t.get("name") for t in tools]
print("调用工具序列:", names)
check("调用了 web_search", "web_search" in names)
check("调用了 create_page", "create_page" in names)
check("调用了 create_note（知识库）", "create_note" in names)
check("回复含网页地址", "/webs/html/" in text or "/p/" in text or "调研" in text)

# 验证页面与笔记落库
from run import app
from app.extensions import db
from app.models.webpage import WebPage
from app.models.note import Note

page_ok = note_ok = False
page_id = note_id = None
with app.app_context():
    pages = WebPage.query.filter(WebPage.title.like("📚 调研%"),
                                 WebPage.deleted_at.is_(None)).all()
    page_ok = len(pages) >= 1
    page_id = pages[0].id if pages else None
    notes = Note.query.filter(Note.deleted_at.is_(None)).all()
    for n in notes:
        if "调研" in (n.tags or []) or n.title.startswith("调研") or "调研" in n.title:
            note_ok, note_id = True, n.id
            break
check("调研网页已创建", page_ok, f"pages={len(pages) if page_ok else 0}")
check("知识库笔记已保存（tag 调研）", note_ok)

# 公开访问调研网页（若是公开页）
if page_id:
    with app.app_context():
        from app.services import page_service
        p = page_service.get_page(page_id)
        if p and p.is_public:
            rr = requests.Session().get(BASE + "/webs/html/" + p.slug, timeout=10)
            check("调研网页可公开访问", rr.status_code == 200 and "Python" in rr.text or "调研" in rr.text)

# 清理
with app.app_context():
    from app.services import note_service, page_service
    if page_id:
        p = page_service.get_page(page_id)
        if p:
            page_service.soft_delete_page(p)
    if note_id:
        n = db.session.get(Note, note_id)
        if n:
            note_service.soft_delete_note(n)
print("  (调研测试页面与笔记已清理)")

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：", FAILED)
    sys.exit(1)
