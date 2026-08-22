"""复查临时验证脚本：验证代码复查发现的问题（用后即删）。"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

BASE = "http://127.0.0.1:5000"
s = requests.Session()
HTML = "<html><body><h1>EQ-CHECK</h1></body></html>"
eq_id = None


def restore():
    r = s.post(BASE + "/settings/pages-domain",
               data={"csrf_token": token, "admin_domain": "", "page_domain": ""},
               allow_redirects=False)
    if eq_id:
        s.post(BASE + "/pages/api/delete", json={"page_id": eq_id})
    print("restored, status:", r.status_code)


try:
    r = s.get(BASE + "/login")
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.text)
    token = m.group(1)
    r = s.post(BASE + "/login", data={"csrf_token": token, "username": "admin", "password": "admin123"},
               allow_redirects=False)
    if r.status_code != 302:
        print(f"登录失败 status={r.status_code}"); sys.exit(1)

    # 1) 配置域名
    s.post(BASE + "/settings/pages-domain",
           data={"csrf_token": token, "admin_domain": "admin.eugenstudio.test",
                 "page_domain": "web.eugenstudio.test"}, allow_redirects=False)

    # 2) IPv6 回环 Host → 应放行（本地开发），若 302 则是 _host() 拆分 bug
    r = s.get(BASE + "/", headers={"Host": "[::1]:5000"}, allow_redirects=False)
    print("IPv6 localhost :", r.status_code, r.headers.get("Location"))

    # 3) 公网 IP Host → 设计上守卫应跳转，实际被 fullmatch 放行
    r = s.get(BASE + "/", headers={"Host": "8.8.8.8"}, allow_redirects=False)
    print("public IP      :", r.status_code, r.headers.get("Location"))

    # 4) 内网 IP → 放行（设计如此）
    r = s.get(BASE + "/", headers={"Host": "192.168.1.5"}, allow_redirects=False)
    print("LAN IP         :", r.status_code, r.headers.get("Location"))

    # 5) 其他域名带 query → 302 是否保留 query
    r = s.get(BASE + "/login?next=%2Fpages", headers={"Host": "other.test"}, allow_redirects=False)
    print("other+query    :", r.status_code, r.headers.get("Location"))

    # 6) 等域名误配置（绕过设置页校验，直接写 DB）
    from app import create_app
    from app.services.settings_service import set_setting

    app = create_app()
    with app.app_context():
        set_setting("admin_domain", "same.eugenstudio.test")
        set_setting("page_domain", "same.eugenstudio.test")
    r = s.post(BASE + "/pages/api/save", json={"title": "等域名检查", "slug": "eq-check", "content": HTML})
    eq_id = (r.json().get("data") or {}).get("id")
    print("create page    :", r.status_code, r.json())
    r = s.get(BASE + "/pages", headers={"Host": "same.eugenstudio.test"})
    print("equal /pages   :", r.status_code, "(后台 UI 被路由接管则 404=bug)")
    r = s.get(BASE + "/eq-check", headers={"Host": "same.eugenstudio.test"})
    print("equal /eq-check:", r.status_code, "(公开页正常渲染=路由生效)")
finally:
    restore()
