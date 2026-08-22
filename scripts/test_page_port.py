"""网页站点：只填端口、HTTP 无需证书。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask_login import login_user

from run import app
from app.models.user import User
from app.models.webpage import WebPage
from app.services import page_service
from app.services.install_service import ensure_schema
from app.services.page_site_server import create_page_site_app
from app.services.settings_service import set_setting

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
    user = User.query.order_by(User.id).first()
    check("存在用户", user is not None)
    if user is None:
        sys.exit(1)

    with app.test_request_context("/"):
        login_user(user)
        set_setting("page_port", 18080, user_id=0)
        set_setting("page_host", "10.0.0.8", user_id=0)
        check("page_port_configured", page_service.page_port_configured() == 18080)
        check("page_site_base_url 为 http 不含 https",
              page_service.page_site_base_url() == "http://10.0.0.8:18080")
        page = WebPage(title="t", slug="porttest1", content="<html>ok</html>",
                       is_public=True, enabled=True, user_id=user.id)
        url = page_service.page_public_url(page)
        check("公开页 URL 走端口", url == "http://10.0.0.8:18080/porttest1", url)
        page.is_public = False
        priv = page_service.page_public_url(page)
        check("私有页不走独立端口", "18080" not in priv, priv)

        set_setting("page_port", 0, user_id=0)
        set_setting("page_host", "", user_id=0)
        check("关闭端口后 base 为空", page_service.page_site_base_url() == "")

    site = create_page_site_app(app)
    client = site.test_client()
    pub = page_service.create_page(user.id, title="端口页", content="<h1>hello-port</h1>",
                                   slug=None, is_public=True, enabled=True)
    r = client.get("/" + pub.slug)
    check("独立站点 GET 公开页 200", r.status_code == 200 and b"hello-port" in r.data, str(r.status_code))
    r2 = client.get("/no-such-slug-xyz")
    check("不存在 slug 404", r2.status_code == 404)
    page_service.soft_delete_page(pub)

    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    html = c.get("/settings/").text
    check("设置页有网页端口", "网页端口" in html and "page_port" in html)
    check("设置页说明无需 HTTPS", "不必配证书" in html or "无需 HTTPS" in html)

print(f"\n通过 {len(PASSED)}  失败 {len(FAILED)}")
if FAILED:
    print("失败项：", ", ".join(FAILED))
    sys.exit(1)
