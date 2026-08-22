"""网页生成器功能测试：登录、列表页、创建、公开访问、隐藏、私有、编辑、复制、删除、slug 校验。"""
import re
import sys
import time

import requests
import test_common

BASE = "http://127.0.0.1:5000"
s = requests.Session()
PASSED, FAILED = [], []

HTML_OK = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><title>测试页</title></head>
<body><h1 id="test-marker">灵犀 网页测试</h1></body></html>"""


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


# 等待服务器就绪
for _ in range(30):
    try:
        if s.get(BASE + "/login", timeout=2).status_code == 200:
            break
    except requests.RequestException:
        time.sleep(1)
else:
    print("服务器未就绪"); sys.exit(1)

# ---- 登录 ----
token = test_common.login(s, BASE)
check("登录成功", bool(token))
s.get(BASE + "/")

# ---- 页面入口 ----
r = s.get(BASE + "/pages")
check("网页列表页 200", r.status_code == 200, f"status={r.status_code}")
check("导航含网页入口", "网页" in r.text)
r = s.get(BASE + "/pages/edit")
check("新建编辑页 200", r.status_code == 200 and "HTML 源码" in r.text)

# ---- 创建（手动指定 slug）----
r = s.post(BASE + "/pages/api/save", json={
    "title": "冒烟测试网页", "slug": "smoke-page", "content": HTML_OK,
    "description": "e2e", "is_public": True, "enabled": True,
})
check("创建网页", r.json().get("ok") is True, str(r.json()))
page_id = (r.json().get("data") or {}).get("id")

# slug 冲突
r = s.post(BASE + "/pages/api/save", json={"title": "冲突页", "slug": "smoke-page", "content": HTML_OK})
check("slug 冲突被拒", r.status_code == 400 and r.json().get("ok") is False, str(r.json()))

# slug 非法
r = s.post(BASE + "/pages/api/save", json={"title": "非法页", "slug": "Bad Slug!", "content": HTML_OK})
check("slug 非法被拒", r.status_code == 400, str(r.json()))

# 自动生成 slug
r = s.post(BASE + "/pages/api/save", json={"title": "自动地址页", "content": HTML_OK})
auto_id = (r.json().get("data") or {}).get("id")
auto_slug = (r.json().get("data") or {}).get("slug")
check("自动生成 slug", bool(auto_slug and auto_slug.startswith("p")), str(r.json()))

# ---- 公开访问（登录态 + 匿名态）----
r = s.get(BASE + "/p/smoke-page")
check("公开页登录态 200", r.status_code == 200 and "test-marker" in r.text)
anon = requests.Session()
r = anon.get(BASE + "/p/smoke-page")
check("公开页匿名 200", r.status_code == 200 and "灵犀 网页测试" in r.text)

# ---- 编辑 ----
r = s.post(BASE + "/pages/api/save", json={
    "page_id": page_id, "title": "冒烟测试网页(改)",
    "content": HTML_OK.replace("灵犀 网页测试", "灵犀 网页测试-已更新"),
})
check("更新网页", r.json().get("ok") is True, str(r.json()))
r = s.get(BASE + "/p/smoke-page")
check("更新已生效", "已更新" in r.text)

# ---- 显示开关（后台控制显示）----
r = s.post(BASE + "/pages/api/toggle", json={"page_id": page_id, "enabled": False})
check("隐藏页面", r.json().get("ok") is True and r.json()["data"]["enabled"] is False, str(r.json()))
r = s.get(BASE + "/p/smoke-page")
check("隐藏后前台 404（登录态）", r.status_code == 404, f"status={r.status_code}")
r = s.post(BASE + "/pages/api/toggle", json={"page_id": page_id, "enabled": True})
check("重新显示", r.json().get("ok") is True)

# ---- 私有页面 ----
r = s.post(BASE + "/pages/api/save", json={
    "page_id": page_id, "is_public": False,
})
check("改为私有", r.json().get("ok") is True, str(r.json()))
r = anon.get(BASE + "/p/smoke-page")
check("私有页匿名 404", r.status_code == 404, f"status={r.status_code}")
r = s.get(BASE + "/p/smoke-page")
check("私有页登录态 200", r.status_code == 200)

# ---- 复制 ----
r = s.post(BASE + "/pages/api/duplicate", json={"page_id": page_id})
dup_id = (r.json().get("data") or {}).get("id")
check("复制页面", r.json().get("ok") is True and dup_id, str(r.json()))

# ---- 列表页包含新页面 ----
r = s.get(BASE + "/pages")
check("列表页含新页面", "冒烟测试网页(改)" in r.text)

# ---- 域名分离（Host 模拟：后台域名 + 网页域名 + 守卫）----
r = s.post(BASE + "/settings/pages-domain",
           data={"csrf_token": token, "admin_domain": "admin.eugenstudio.test",
                 "page_domain": "web.eugenstudio.test"},
           allow_redirects=False)
check("保存域名设置", r.status_code == 302, f"实际 {r.status_code}")

# 公开页面：网页域名 /<slug>（匿名/登录均可，含 /p/ 别名）
s.post(BASE + "/pages/api/save", json={"page_id": page_id, "is_public": True})
r = s.get(BASE + "/smoke-page", headers={"Host": "web.eugenstudio.test"})
check("网页域名 /<slug> 渲染", r.status_code == 200 and "灵犀 网页测试" in r.text, f"status={r.status_code}")
r = anon.get(BASE + "/smoke-page", headers={"Host": "web.eugenstudio.test"})
check("网页域名公开页匿名 200", r.status_code == 200, f"status={r.status_code}")
r = anon.get(BASE + "/p/smoke-page", headers={"Host": "web.eugenstudio.test"})
check("网页域名 /p/<slug> 别名", r.status_code == 200, f"status={r.status_code}")

# 网页域名不提供后台能力
r = anon.get(BASE + "/login", headers={"Host": "web.eugenstudio.test"})
check("网页域名后台路径 404", r.status_code == 404, f"status={r.status_code}")

# 私有页面：网页域名 404；后台域名 /p/<slug> 登录可见
s.post(BASE + "/pages/api/save", json={"page_id": page_id, "is_public": False})
r = s.get(BASE + "/smoke-page", headers={"Host": "web.eugenstudio.test"})
check("网页域名私有页 404", r.status_code == 404, f"status={r.status_code}")
# requests 按 Host 头匹配 cookie 域：为模拟的后台域名注入同一会话 cookie（值本身与域名无关）
_ses = s.cookies.get("session")
if _ses:
    s.cookies.set("session", _ses, domain="admin.eugenstudio.test", path="/")
r = s.get(BASE + "/p/smoke-page", headers={"Host": "admin.eugenstudio.test"})
check("后台域名私有页登录态 200", r.status_code == 200, f"status={r.status_code}")
r = anon.get(BASE + "/p/smoke-page", headers={"Host": "admin.eugenstudio.test"})
check("后台域名私有页匿名 404", r.status_code == 404, f"status={r.status_code}")

# 后台域名守卫：其他域名 → 302 跳转后台域名
r = s.get(BASE + "/", headers={"Host": "other.eugenstudio.test"}, allow_redirects=False)
check("非后台域名 302 跳转", r.status_code == 302
      and (r.headers.get("Location") or "").startswith("https://admin.eugenstudio.test/"),
      f"status={r.status_code} loc={r.headers.get('Location')}")
r = s.get(BASE + "/p/smoke-page", headers={"Host": "other.eugenstudio.test"}, allow_redirects=False)
check("非后台域名访问页面也跳转", r.status_code == 302, f"status={r.status_code}")

# 列表页展示按域名体系计算的地址（此时页面为私有 → 后台域名 /p/<slug>）
r = s.get(BASE + "/pages")
check("列表页显示后台域名地址", "https://admin.eugenstudio.test/p/smoke-page" in r.text)

# 清理域名配置（恢复默认路径模式）
s.post(BASE + "/settings/pages-domain",
       data={"csrf_token": token, "admin_domain": "", "page_domain": ""},
       allow_redirects=False)

# ---- 删除（软删除 + slug 释放）----
r = s.post(BASE + "/pages/api/delete", json={"page_id": page_id})
check("软删除页面", r.json().get("ok") is True, str(r.json()))
r = s.get(BASE + "/p/smoke-page")
check("删除后 404", r.status_code == 404, f"status={r.status_code}")
r = s.post(BASE + "/pages/api/save", json={"title": "复用地址页", "slug": "smoke-page", "content": HTML_OK})
check("删除后 slug 可复用", r.json().get("ok") is True, str(r.json()))
reuse_id = (r.json().get("data") or {}).get("id")

# ---- 清理 ----
for pid in (page_id, auto_id, dup_id, reuse_id):
    if pid:
        s.post(BASE + "/pages/api/delete", json={"page_id": pid})
print("  (测试数据已软删除)")

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：", FAILED)
    sys.exit(1)
