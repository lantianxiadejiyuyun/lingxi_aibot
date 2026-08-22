"""检查新 Grid 布局元素是否出现在各页面。"""
import re
import requests

BASE = "http://127.0.0.1:5000"
s = requests.Session()
r = s.get(BASE + "/login")
token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.text).group(1)
s.post(BASE + "/login", data={"csrf_token": token, "username": "admin", "password": "admin123"})

html = s.get(BASE + "/").text
checks = {
    "仪表盘 grid-3": 'class="grid grid-3"' in html,
    "col-span-2 大卡片": 'col-span-2' in html,
    "快捷操作网格": 'quick-actions' in html and 'quick-action' in html,
    "顶栏内层容器": 'topbar-inner' in html,
}
for name, ok in checks.items():
    print(("✓" if ok else "✗"), name)

css = requests.get(BASE + "/static/css/style.css").text
for cls in [".grid-3", ".grid-4", ".col-span-2", ".grid-auto", ".topbar-inner", ".quick-actions"]:
    print(("✓" if cls in css else "✗"), "CSS", cls)

settings = s.get(BASE + "/settings").text
print(("✓" if "form-row" in settings and "llm_base_url" in settings else "✗"), "设置页 AI 表单两列")
