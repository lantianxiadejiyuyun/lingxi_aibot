"""验证宽屏/大屏适配 CSS 规则存在且页面正常。"""
import re
import requests

BASE = "http://127.0.0.1:5000"
s = requests.Session()
r = s.get(BASE + "/login")
token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.text).group(1)
s.post(BASE + "/login", data={"csrf_token": token, "username": "admin", "password": "admin123"})

css = requests.get(BASE + "/static/css/style.css").text
ok = True
for cls in ["@media (min-width: 1440px)", "@media (min-width: 1920px)",
            "calc(100vw - 64px)", "calc(100vw - 96px)",
            "minmax(0, 1fr)", ".grid-5", ".grid-6",
            "minmax(300px, 1fr)", "248px"]:
    hit = cls in css
    ok = ok and hit
    print(("✓" if hit else "✗"), "CSS", cls)

# 所有页面仍正常渲染
for path in ["/", "/calendar/", "/tasks/", "/notes/", "/chat/", "/jobs/", "/notifications/", "/settings/"]:
    r = s.get(BASE + path)
    hit = r.status_code == 200
    ok = ok and hit
    print(("✓" if hit else "✗"), "页面", path)

print("\n全部通过" if ok else "\n存在失败项")
raise SystemExit(0 if ok else 1)
