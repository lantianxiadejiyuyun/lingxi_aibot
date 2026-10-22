"""安装引导页测试：已初始化自动跳转；未初始化显示可视化安装向导（数据库配置→初始化→管理员）。"""
import os
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


# ---- 已初始化系统：/setup 自动跳转 ----
anon = requests.Session()
r = anon.get(BASE + "/setup", allow_redirects=False)
check("匿名访问 /setup 302 跳登录", r.status_code == 302
      and (r.headers.get("Location") or "").endswith("/login"),
      f"status={r.status_code} loc={r.headers.get('Location')}")

s = requests.Session()
token = test_common.login(s, BASE)
check("登录成功", bool(token))
r = s.get(BASE + "/setup", allow_redirects=False)
check("登录后访问 /setup 302 跳仪表盘", r.status_code == 302
      and (r.headers.get("Location") or "").endswith("/"),
      f"status={r.status_code} loc={r.headers.get('Location')}")

# 导航不再含安装配置入口
r = s.get(BASE + "/")
check("导航不含安装配置入口", "安装配置" not in r.text)

# 已初始化：登录页不显示安装引导提示
r = anon.get(BASE + "/login")
check("已初始化登录页无引导提示", "查看安装引导" not in r.text)

# ---- 进程内：模拟未初始化，验证安装向导各步骤渲染与跳转 ----
from run import app
import app.blueprints.setup as setup_mod

with app.app_context():
    check("system_initialized() 为 True（真实库）", setup_mod.system_initialized() is True)

    # 模拟数据库不可用 → 所有页面跳 /setup，/setup 显示数据库配置表单
    setup_mod.install_status = lambda: {"db_ok": False, "tables_ok": False, "admin_ok": False}
    c = app.test_client()

    r = c.get("/")
    check("未初始化首页 302 跳安装向导", r.status_code == 302
          and (r.headers.get("Location") or "").endswith("/setup"),
          f"status={r.status_code} loc={r.headers.get('Location')}")
    r = c.get("/login")
    check("未初始化登录页 302 跳安装向导", r.status_code == 302
          and (r.headers.get("Location") or "").endswith("/setup"),
          f"status={r.status_code} loc={r.headers.get('Location')}")

    r = c.get("/setup")
    text = r.get_data(as_text=True)
    ok = r.status_code == 200 and "安装引导" in text and "配置数据库连接" in text
    check("未初始化显示数据库配置向导", ok, f"status={r.status_code}")
    check("向导含测试连接按钮", "btn-db-test" in text)
    check("向导含保存配置按钮", "btn-db-save" in text)

    # 模拟已连库未建表 → 显示初始化步骤
    setup_mod.install_status = lambda: {"db_ok": True, "tables_ok": False, "admin_ok": False}
    r = c.get("/setup")
    text = r.get_data(as_text=True)
    check("已连库显示初始化步骤", "开始初始化" in text and "btn-init" in text)

    # 模拟已建表无管理员 → 显示管理员创建步骤
    setup_mod.install_status = lambda: {"db_ok": True, "tables_ok": True, "admin_ok": False}
    r = c.get("/setup")
    text = r.get_data(as_text=True)
    check("已建表显示管理员步骤", "创建管理员账号" in text and "btn-admin" in text)

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：", FAILED)
    sys.exit(1)
