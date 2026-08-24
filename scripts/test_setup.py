"""安装引导页测试：/setup 始终显示向导、从第①步开始；写接口只校验令牌。"""
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


# ---- 已初始化系统：/setup 仍显示向导（需活服务，连不上则跳过） ----
try:
    anon = requests.Session()
    r = anon.get(BASE + "/setup", allow_redirects=False, timeout=2)
    check("匿名访问 /setup 仍显示向导", r.status_code == 200
          and "安装引导" in r.text and "配置数据库连接" in r.text,
          f"status={r.status_code}")

    s = requests.Session()
    token = test_common.login(s, BASE)
    check("登录成功", bool(token))
    r = s.get(BASE + "/setup", allow_redirects=False)
    check("登录后访问 /setup 仍显示向导", r.status_code == 200
          and "安装引导" in r.text,
          f"status={r.status_code} loc={r.headers.get('Location')}")

    r = s.get(BASE + "/")
    check("导航不含安装配置入口", "安装配置" not in r.text)

    r = anon.get(BASE + "/login")
    check("已初始化登录页无引导提示", "查看安装引导" not in r.text)
except requests.exceptions.ConnectionError:
    print("  ↷ 活服务未启动，跳过已初始化访问项")

# ---- 进程内：模拟未初始化，验证安装向导各步骤渲染与跳转 ----
from run import app
import app.blueprints.setup as setup_mod

with app.app_context():
    from app.services import install_service as inst
    orig_drop = inst.drop_all_tables
    inst.drop_all_tables = lambda: (_ for _ in ()).throw(
        AssertionError("测试不得真实删表"))
    check("system_initialized() 为 True（真实库）", setup_mod.system_initialized() is True)
    c_login = app.test_client()
    r = c_login.get("/setup", follow_redirects=False)
    text = r.get_data(as_text=True)
    check("已初始化仍显示安装向导", r.status_code == 200 and "配置数据库连接" in text,
          f"status={r.status_code}")
    check("每次打开从第①步开始",
          "var wizardStep = 1" in text and "function currentStep()" in text)
    r = c_login.post("/setup/api/reset-db", json={})
    body = r.get_json() or {}
    check("无令牌拒绝重置数据库", r.status_code == 403 and body.get("ok") is False,
          f"status={r.status_code} body={body}")
    r = c_login.get("/login?installed=1")
    text = r.get_data(as_text=True)
    check("安装完成后登录页提示收藏", r.status_code == 200 and "请收藏当前页面" in text,
          f"status={r.status_code}")
    check("登录页带复制地址按钮", "btn-copy-login-url" in text)

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
    check("向导含安装令牌确认按钮", "btn-token" in text and "/setup/api/verify-token" in text)
    check("向导含重置数据库按钮", "btn-reset-db" in text and "/setup/api/reset-db" in text)
    check("重置数据库需二次确认", "window.confirm" in text and "全部数据表" in text)
    check("步骤面板用 is-on 显示，避免按钮被 CSS 藏掉",
          "setup-panel.is-on" in text and 'classList.toggle("is-on"' in text)

    token = setup_mod._get_or_create_token()
    r = c.post("/setup/api/verify-token", json={"setup_token": token})
    body = r.get_json() or {}
    check("正确安装令牌校验通过", r.status_code == 200 and body.get("ok") is True,
          f"status={r.status_code} body={body}")
    r = c.post("/setup/api/verify-token", json={"setup_token": "wrong-token"})
    check("错误安装令牌拒绝", r.status_code == 403)
    r = c.post("/setup/api/verify-token", json={})
    check("空安装令牌拒绝", r.status_code == 403)

    orig_connectable = setup_mod._db_connectable
    try:
        r = c.post("/setup/api/reset-db", json={"setup_token": "wrong-token"})
        check("重置数据库令牌错误拒绝", r.status_code == 403)

        setup_mod._db_connectable = lambda: False
        r = c.post("/setup/api/reset-db", json={"setup_token": token})
        body = r.get_json() or {}
        check("数据库未连接拒绝重置", r.status_code == 400
              and "未连接" in str(body.get("error") or ""),
              f"status={r.status_code} body={body}")
        setup_mod._db_connectable = orig_connectable

        dropped_called = []

        def _fake_drop():
            dropped_called.append(True)
            return ["users", "events"]

        inst.drop_all_tables = _fake_drop
        setup_mod.install_status = lambda: {
            "db_ok": True, "tables_ok": True, "admin_ok": False}
        r = c.post("/setup/api/reset-db", json={"setup_token": token})
        body = r.get_json() or {}
        check("令牌正确即可重置数据库",
              r.status_code == 200 and body.get("ok") is True and dropped_called,
              f"status={r.status_code} body={body}")
        check("重置后回到未建表状态",
              body.get("tables_ok") is False and body.get("admin_ok") is False
              and body.get("dropped") == ["users", "events"],
              str(body))
        check("重置成功提示删除张数", "2 张" in str(body.get("message") or ""),
              str(body.get("message")))
        inst.drop_all_tables = lambda: (_ for _ in ()).throw(
            AssertionError("测试不得真实删表"))
    finally:
        setup_mod._db_connectable = orig_connectable

    # 模拟已连库未建表 → 显示初始化步骤
    setup_mod.install_status = lambda: {"db_ok": True, "tables_ok": False, "admin_ok": False}
    r = c.get("/setup")
    text = r.get_data(as_text=True)
    check("已连库仍渲染全部步骤（打开停在第①步）",
          "配置数据库连接" in text and "开始初始化" in text and "btn-init" in text)

    setup_mod.install_status = lambda: {"db_ok": True, "tables_ok": True, "admin_ok": False}
    r = c.get("/setup")
    text = r.get_data(as_text=True)
    check("已建表仍从第①步渲染", "配置数据库连接" in text and "tables-exist-hint" in text
          and "var wizardStep = 1" in text)
    check("步进不读取管理员状态", "function currentStep()" in text
          and "admin_ok" not in text.split("function currentStep()")[1].split("function renderWizard")[0])
    check("向导含管理员步骤（点初始化后才进入）", "创建管理员账号" in text and "btn-admin" in text)
    check("不再提供返回初始化按钮", "btn-back-init" not in text)
    check("向导一次渲染全部步骤（避免保存后刷新丢状态）",
          'data-panel="1"' in text and 'data-panel="4"' in text
          and "lingxi-setup-token" in text and "location.reload()" not in text)

    setup_mod.install_status = lambda: {"db_ok": True, "tables_ok": True, "admin_ok": True}
    dropped_called = []

    def _fake_drop_admin():
        dropped_called.append(True)
        return ["users"]

    inst.drop_all_tables = _fake_drop_admin
    try:
        r = c.post("/setup/api/reset-db", json={"setup_token": token})
        body = r.get_json() or {}
        check("已有管理员+令牌仍可重置数据库",
              r.status_code == 200 and body.get("ok") is True and dropped_called,
              f"status={r.status_code} body={body}")
    finally:
        inst.drop_all_tables = orig_drop
    c2 = app.test_client()
    r = c2.get("/setup", follow_redirects=False)
    text = r.get_data(as_text=True)
    check("已初始化访问 /setup 不跳转",
          r.status_code == 200 and "配置数据库连接" in text and "var wizardStep = 1" in text,
          f"status={r.status_code} loc={r.headers.get('Location')}")
    r = c2.post("/setup/api/finish", json={"setup_token": token})
    body = r.get_json() or {}
    check("finish 结束向导", r.status_code == 200 and body.get("ok") is True)
    check("finish 返回登录地址", bool(body.get("login_url")) and "login" in str(body.get("login_url")),
          str(body))
    check("finish 登录地址提示收藏", "installed=1" in str(body.get("login_url") or ""),
          str(body.get("login_url")))
    r = c2.get("/setup", follow_redirects=False)
    text = r.get_data(as_text=True)
    check("结束向导后再访 /setup 仍显示向导",
          r.status_code == 200 and "配置数据库连接" in text,
          f"status={r.status_code}")

# ---- 热加载数据库配置：保存后内存 MYSQL_* 必须立刻更新 ----
import os
from app.services.install_service import apply_runtime_db_config, rebuild_engine

with app.app_context():
    cfg = app.config
    keys = ("MYSQL_HOST", "MYSQL_PORT", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DB",
            "SQLALCHEMY_DATABASE_URI")
    orig = {k: cfg.get(k) for k in keys}
    orig_env = {k: os.environ.get(k) for k in
                ("MYSQL_HOST", "MYSQL_PORT", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DB")}
    try:
        apply_runtime_db_config("127.0.0.1", 3307, "wizard_user", "wizard_pass", "wizard_db")
        check("热加载写入 MYSQL_USER", cfg.get("MYSQL_USER") == "wizard_user")
        check("热加载写入 MYSQL_DB", cfg.get("MYSQL_DB") == "wizard_db")
        check("热加载更新连接串", "wizard_user" in (cfg.get("SQLALCHEMY_DATABASE_URI") or "")
              and "wizard_db" in (cfg.get("SQLALCHEMY_DATABASE_URI") or ""))
    finally:
        for k, v in orig.items():
            cfg[k] = v
        for k, v in orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        rebuild_engine(orig["MYSQL_HOST"], orig["MYSQL_PORT"], orig["MYSQL_USER"],
                       orig["MYSQL_PASSWORD"] or "", orig["MYSQL_DB"])

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：", FAILED)
    sys.exit(1)
