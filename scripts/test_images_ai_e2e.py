"""图片 AI 端到端：对话生成图片 + 对话改图（走 mock 图服务，消耗 LLM 调用）。"""
import json
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


def extract_tools(text):
    tools = []
    for line in text.split("\n"):
        if line.startswith("data:") and '"name"' in line:
            try:
                tools.append(json.loads(line[5:].strip()))
            except ValueError:
                pass
    return tools


# ---- 1. 对话生成图片 ----
print("① 对话生成图片…")
r = s.post(BASE + "/chat/api/send",
           json={"message": "帮我画一张图：一只戴着宇航头盔的柴犬漂浮在星空，卡通风格。画好后把图片链接发给我。"},
           timeout=180)
tools = extract_tools(r.text)
gen = [t for t in tools if t.get("name") == "generate_image"]
print("调用 generate_image:", "✓" if gen else "✗")
img_id = None
img_url = None
if gen:
    try:
        res = json.loads(gen[0].get("result") or "{}")
        img_id = res.get("id")
        img_url = res.get("url")
    except ValueError:
        pass
m_url = re.search(r'http[^"\s)]*/img/[a-f0-9-]+\.(?:png|jpe?g|webp|gif)', r.text)
img_url = img_url or (m_url.group(0) if m_url else None)
print("工具返回图片 URL:", "✓" if img_url else "✗", img_url or "")
if img_url:
    rr = s.get(img_url)
    print("图片可访问:", "✓" if rr.status_code == 200 and rr.headers.get("Content-Type", "").startswith("image/")
          else f"✗ status={rr.status_code}")

# ---- 2. 对话改图 ----
print("\n② 对话改图…")
edit_msg = f"把刚才生成的那张柴犬图片（id={img_id}）的背景改成黄昏的天空"
r = s.post(BASE + "/chat/api/send", json={"message": edit_msg}, timeout=180)
tools2 = extract_tools(r.text)
ed = [t for t in tools2 if t.get("name") == "edit_image"]
print("调用 edit_image:", "✓" if ed else "✗", "| 备用 list_images:",
      "✓" if any(t.get("name") == "list_images" for t in tools2) else "✗")
edit_url = None
if ed:
    try:
        res = json.loads(ed[0].get("result") or "{}")
        edit_url = res.get("url")
        print("改图关联 parent_id:", res.get("parent_id"))
    except ValueError:
        pass
m2 = re.search(r'http[^"\s)]*/img/[a-f0-9-]+\.(?:png|jpe?g|webp|gif)', r.text)
edit_url = edit_url or (m2.group(0) if m2 else None)
if edit_url:
    rr = s.get(edit_url)
    print("改图结果可访问:", "✓" if rr.status_code == 200 else f"✗ status={rr.status_code}")

# ---- 清理 ----
ids = sorted(set(re.findall(r'data-delete-image="(\d+)"', s.get(BASE + "/images").text)))
for i in ids:
    s.post(BASE + "/images/api/delete", json={"image_id": i})
print("\n清理图片:", ids)

ok = bool(gen) and bool(img_url) and bool(ed or any(t.get("name") == "list_images" for t in tools2))
print("AI 图片端到端:", "全部通过 ✓" if ok else "存在失败项 ✗")
sys.exit(0 if ok else 1)
