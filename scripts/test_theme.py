"""主题偏好：写入 users.prefs，API 校验，页面带上 data-theme-pref。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run import app
from app.extensions import db
from app.models.user import DEFAULT_THEME, THEMES, User
from app.services.install_service import ensure_schema
from sqlalchemy.orm.attributes import flag_modified

PASSED, FAILED = [], []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


with app.app_context():
    ensure_schema()
    user = User.query.filter_by(is_admin=True).order_by(User.id).first() \
        or User.query.order_by(User.id).first()
    check("存在可用用户", user is not None)
    if user is None:
        sys.exit(1)

    original = dict(user.prefs or {})
    try:
        user.prefs = {}
        flag_modified(user, "prefs")
        db.session.commit()
        db.session.refresh(user)
        check("未设置时默认浅色", user.get_theme() == DEFAULT_THEME, user.get_theme())

        saved = user.set_theme("dark")
        flag_modified(user, "prefs")
        db.session.commit()
        db.session.refresh(user)
        check("set_theme 返回 dark", saved == "dark")
        check("prefs 写入 theme", (user.prefs or {}).get("theme") == "dark", str(user.prefs))
        check("get_theme 读出 dark", user.get_theme() == "dark")

        user.set_theme("not-a-theme")
        check("非法主题回退默认", user.get_theme() == DEFAULT_THEME, user.get_theme())
        user.set_theme("system")
        flag_modified(user, "prefs")
        db.session.commit()
        check("system 合法", user.get_theme() == "system")
        check("THEMES 三项", THEMES == ("light", "dark", "system"))

        prev_testing = app.config.get("TESTING")
        prev_secure = app.config.get("SESSION_COOKIE_SECURE")
        app.config["TESTING"] = True
        app.config["SESSION_COOKIE_SECURE"] = False
        try:
            with app.test_client() as client:
                with client.session_transaction() as sess:
                    sess["_user_id"] = str(user.id)
                    sess["_fresh"] = True

                bad = client.post("/settings/api/theme", json={"theme": "neon"})
                check("非法主题 400", bad.status_code == 400, str(bad.status_code))

                ok = client.post("/settings/api/theme", json={"theme": "dark"})
                body = ok.get_json(silent=True) or {}
                check("保存 dark 200", ok.status_code == 200, str(ok.status_code))
                check("API 返回 ok", body.get("ok") is True, str(body))
                check("API 回写 dark", (body.get("data") or {}).get("theme") == "dark", str(body))

                db.session.refresh(user)
                check("账号持久化 dark", user.get_theme() == "dark", str(user.prefs))

                page = client.get("/")
                html = page.get_data(as_text=True)
                check("仪表盘 200", page.status_code == 200, str(page.status_code))
                check("页面带 data-theme-pref=dark", 'data-theme-pref="dark"' in html, html[:400])
                check("顶栏有主题切换", "data-theme-switch" in html and 'data-theme-set="dark"' in html)

                settings = client.get("/settings/")
                shtml = settings.get_data(as_text=True)
                check("设置页有外观选项", "data-theme-picks" in shtml and "跟随系统" in shtml)

            from flask import render_template

            from app.blueprints.auth import LoginForm

            with app.test_request_context("/login"):
                lhtml = render_template(
                    "auth/login.html", form=LoginForm(), setup_required=False)
            check("登录页有主题按钮", "data-theme-switch" in lhtml)
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            base_html = open(os.path.join(root, "app", "templates", "base.html"),
                             encoding="utf-8").read()
            check("未登录允许空主题偏好",
                  'data-theme-pref="{{ ui_theme|default(\'\') }}"' in base_html)
            check("未登录回退 localStorage", "localStorage.getItem(KEY)" in base_html)
        finally:
            app.config["TESTING"] = prev_testing
            app.config["SESSION_COOKIE_SECURE"] = prev_secure
    finally:
        user.prefs = original
        flag_modified(user, "prefs")
        db.session.commit()

print(f"\n通过 {len(PASSED)}  失败 {len(FAILED)}")
if FAILED:
    print("失败项:", FAILED)
sys.exit(0 if not FAILED else 1)
