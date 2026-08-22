"""图片生成功能测试：页面 / 配置状态 / 路径安全 / 生成 / 访问控制 / 公开开关 / AI 改图 / 删除。

- 未配置图片服务时只测基础与错误路径；已配置时进行真实生成与改图（消耗配额）。
"""
import re
import sys
import time

import requests
import test_common

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


def absu(url):
    """API 返回的 URL 可能是绝对地址，统一处理。"""
    return url if str(url or "").startswith("http") else BASE + str(url or "")


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

# ---- 页面与入口 ----
r = s.get(BASE + "/images")
check("图片库页 200", r.status_code == 200, f"status={r.status_code}")
check("导航含图片入口", "🖼️" in r.text)
r = s.get(BASE + "/settings")
check("设置页含图片生成 Tab", "图片生成" in r.text and "image_base_url" in r.text)

# ---- 路径安全 ----
anon = requests.Session()
for bad in ("/img/..%2F..%2Fapp%2Fconfig.py", "/img/not-exist.png", "/img/img-00000000-000000.png"):
    r = anon.get(BASE + bad)
    check(f"非法路径 404: {bad}", r.status_code == 404, f"status={r.status_code}")

# ---- 配置状态探测 ----
r = s.post(BASE + "/images/api/generate", json={"prompt": "", "size": "1024x1024"})
err = (r.json() or {}).get("error", "")
configured = "未配置" not in err
print(f"  图片服务配置状态: {'已配置' if configured else '未配置'}")
check("空提示词被拒", r.status_code == 400, str(r.json()))

if not configured:
    print("  未配置图片服务，跳过真实生成/改图（先到「设置 → 图片生成」配置）")
    print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项（真实生成未测）")
    sys.exit(1 if FAILED else 0)

# ---- 真实生成 ----
r = s.post(BASE + "/images/api/generate",
           json={"prompt": "一只戴宇航头盔的柴犬，卡通风格，测试图片", "size": "1024x1024"})
check("生成图片", r.json().get("ok") is True, str(r.json())[:200])
img_id = (r.json().get("data") or {}).get("id")
img_url = (r.json().get("data") or {}).get("url")
check("返回图片 URL", bool(img_url), str(r.json())[:200])

# ---- 访问控制 ----
r = s.get(absu(img_url))
ct = r.headers.get("Content-Type", "")
check("登录态可访问（图片内容）", r.status_code == 200 and ct.startswith("image/"), f"status={r.status_code} ct={ct}")
r = anon.get(absu(img_url))
check("私有图片匿名 404", r.status_code == 404, f"status={r.status_code}")

# ---- 公开开关 ----
r = s.post(BASE + "/images/api/toggle-public", json={"image_id": img_id, "is_public": True})
check("设为公开", r.json().get("ok") is True, str(r.json()))
r = anon.get(absu(img_url))
check("公开图片匿名 200", r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image/"),
      f"status={r.status_code}")
r = s.post(BASE + "/images/api/toggle-public", json={"image_id": img_id, "is_public": False})
check("设回私有", r.json().get("ok") is True)

# ---- AI 改图（编辑接口或降级重生成）----
r = s.post(BASE + "/images/api/edit", json={"image_id": img_id, "instruction": "把背景改成黄昏的天空"})
check("AI 改图", r.json().get("ok") is True, str(r.json())[:200])
edit_id = (r.json().get("data") or {}).get("id")
edit_parent = (r.json().get("data") or {}).get("parent_id")
check("改图关联原图", str(edit_parent) == str(img_id), f"parent_id={edit_parent}")
edit_url = (r.json().get("data") or {}).get("url")
r = s.get(absu(edit_url))
check("改图结果可访问", r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image/"),
      f"status={r.status_code}")

# ---- 列表页包含 ----
r = s.get(BASE + "/images")
check("图片库页包含新图", "戴宇航头盔" in r.text)

# ---- 清理 ----
for pid in (img_id, edit_id):
    if pid:
        r = s.post(BASE + "/images/api/delete", json={"image_id": pid})
        check(f"删除图片 {pid}", r.json().get("ok") is True, str(r.json()))
print("  (测试图片已软删除)")

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：", FAILED)
    sys.exit(1)
