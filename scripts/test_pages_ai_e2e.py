"""AI 端到端：让 AI 通过 create_page 工具真实生成网页，验证后清理（会消耗一次 LLM 调用）。"""
import re
import sys

import requests

BASE = "http://127.0.0.1:5000"
s = requests.Session()

# 登录
r = s.get(BASE + "/login")
m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.text)
token = m.group(1) if m else ""
r = s.post(BASE + "/login", data={"csrf_token": token, "username": "admin", "password": "admin123"},
           allow_redirects=False)
if r.status_code != 302:
    print("登录失败"); sys.exit(1)
s.headers["X-CSRFToken"] = token

# 让 AI 生成网页
prompt = ("帮我创建一个网页：标题是「AI 测试网页」，内容是一个简单的个人主页 HTML"
          "（含 <!DOCTYPE html>、h1 标题和一句自我介绍，样式内联），"
          "slug 用 ai-test-page，公开访问。")
print("发送对话…")
r = s.post(BASE + "/chat/api/send", json={"message": prompt}, timeout=180)
print("SSE 事件摘要：")
for line in r.text.split("\n"):
    if line.startswith(("event:", "data:")):
        print("  " + line[:180])

used_tool = "create_page" in r.text
print("\n调用 create_page 工具:", "✓" if used_tool else "✗")

# 验证页面存在且公开可访问
r = s.get(BASE + "/pages")
row = ""
for mm in re.finditer(r'<tr data-page-row="(\d+)">([\s\S]*?)</tr>', r.text):
    if "AI 测试网页" in mm.group(2) or "ai-test-page" in mm.group(2):
        row, pid = mm.group(2), mm.group(1)
        break
else:
    print("列表页未找到「AI 测试网页」"); sys.exit(1)
print("列表页包含新页面:", "✓")

r = s.get(BASE + "/webs/html/ai-test-page")
ok = r.status_code == 200 and ("h1" in r.text or "自我介绍" in r.text)
print(f"公开访问 /webs/html/ai-test-page: {r.status_code}", "✓" if ok else "✗")
print("  内容前 150 字:", r.text[:150].replace("\n", "|"))

# 清理
r = s.post(BASE + "/pages/api/delete", json={"page_id": pid})
print("清理:", r.json())

if used_tool and ok:
    print("\nAI 端到端：全部通过 ✓")
    sys.exit(0)
print("\nAI 端到端：存在失败项 ✗")
sys.exit(1)
