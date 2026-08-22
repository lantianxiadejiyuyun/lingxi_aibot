"""飞书接入方式：HTTP 回调 vs 官方 SDK 长连接（配置 / 状态 / HTTP 在 SDK 模式下不重复处理）。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask_login import login_user

from run import app
from app.extensions import db
from app.models.user import User
from app.services.feishu_inbound import mark_seen, parse_message
from app.services.feishu_ws import MODE_CALLBACK, MODE_SDK, receive_mode, sdk_available, start_if_needed, status
from app.services.install_service import ensure_schema
from app.services.settings_service import set_setting

PASSED, FAILED = [], []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


PAYLOAD = {
    "schema": "2.0",
    "header": {"event_type": "im.message.receive_v1", "token": "vt-test"},
    "event": {
        "sender": {"sender_id": {"open_id": "ou_test_receive"}},
        "message": {
            "message_id": "om_receive_unit_1",
            "chat_id": "oc_receive_unit",
            "message_type": "text",
            "content": '{"text":"你好"}',
        },
    },
}


with app.app_context():
    ensure_schema()
    user = User.query.filter_by(is_admin=True).order_by(User.id).first() \
        or User.query.order_by(User.id).first()
    check("存在可用用户", user is not None)
    if user is None:
        sys.exit(1)

    with app.test_request_context("/"):
        login_user(user)
        set_setting("feishu_receive_mode", MODE_CALLBACK, user_id=0)
        set_setting("feishu_receive_mode", MODE_CALLBACK, user_id=user.id)
        set_setting("feishu_event_token", "vt-test", user_id=0)
        set_setting("feishu_event_encrypt_key", "", user_id=0)
        set_setting("feishu_event_encrypt_key", "", user_id=user.id)

        check("默认接入 callback", receive_mode() == MODE_CALLBACK)
        msg = parse_message(PAYLOAD)
        check("parse_message 文本", msg and msg["type"] == "text" and msg["text"] == "你好", str(msg))
        check("parse_message 提取 open_id", msg.get("open_id") == "ou_test_receive")
        img = dict(PAYLOAD)
        img["event"] = dict(PAYLOAD["event"])
        img["event"]["message"] = {
            "message_id": "om_img", "chat_id": "oc_x",
            "message_type": "image", "content": '{"image_key":"img_key"}',
        }
        im = parse_message(img)
        check("parse_message 图片", im and im["type"] == "image" and im["image_key"] == "img_key")
        check("非消息事件忽略", parse_message({"header": {"event_type": "other"}}) is None)

        check("mark_seen 首次 True", mark_seen("om_unique_xyz") is True)
        check("mark_seen 重复 False", mark_seen("om_unique_xyz") is False)

        st = status()
        check("status 含 mode/sdk_installed", "mode" in st and "sdk_installed" in st, str(st))
        check("sdk_available 布尔", isinstance(sdk_available(), bool))

        start_if_needed(app)
        check("callback 模式不启长连接", status()["running"] is False)

        set_setting("feishu_receive_mode", MODE_SDK, user_id=0)
        check("切到 sdk", receive_mode() == MODE_SDK)
        start_if_needed(app)
        # 无 App ID 时不应假称已连接
        check("sdk 无凭证时有错误提示", bool(status().get("error")), str(status()))

        set_setting("feishu_receive_mode", MODE_CALLBACK, user_id=0)
        start_if_needed(app)

    client = app.test_client()
    r = client.post("/feishu/event", json={
        "type": "url_verification", "token": "vt-test", "challenge": "c-1",
    })
    check("callback 握手 challenge", r.status_code == 200 and (r.get_json() or {}).get("challenge") == "c-1",
          str(r.status_code) + str(r.get_json()))

    with app.test_request_context("/"):
        login_user(user)
        set_setting("feishu_receive_mode", MODE_SDK, user_id=0)
    r = client.post("/feishu/event", json={
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1", "token": "vt-test"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_x"}},
            "message": {
                "message_id": "om_sdk_mode_skip",
                "chat_id": "oc_skip",
                "message_type": "text",
                "content": '{"text":"不应处理"}',
            },
        },
    })
    check("SDK 模式 HTTP 消息仍 200", r.status_code == 200 and (r.get_json() or {}).get("code") == 0)

    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    page = client.get("/settings/")
    check("设置页含接入方式", page.status_code == 200 and "官方 SDK 长连接" in page.text and "HTTP 回调" in page.text)
    check("设置页含 lark-oapi 说明", "lark-oapi" in page.text)
    check("设置页 SDK 提示构建网页需公网 IPv4", "无法打开构建的网页" in page.text or "公网 IPv4" in page.text)

    from app.utils import netinfo as netinfo_mod
    from app.utils.netinfo import PAGE_UNREACHABLE_MSG, feishu_sdk_page_warning, reply_looks_like_page
    check("访问地址视为网页回复", reply_looks_like_page("访问地址：https://example.com/p/x"))
    check("普通回复不视为网页", not reply_looks_like_page("今天天气不错"))
    with app.test_request_context("/"):
        login_user(user)
        set_setting("page_domain", "", user_id=0)
        set_setting("admin_domain", "", user_id=0)
        netinfo_mod._cache["data"] = None
        set_setting("feishu_receive_mode", MODE_CALLBACK, user_id=0)
        check("callback 模式不附加网页警告", feishu_sdk_page_warning() == "")
        set_setting("feishu_receive_mode", MODE_SDK, user_id=0)
        netinfo_mod._cache["data"] = None
        warn = feishu_sdk_page_warning()
        check("sdk 且无公网网页域名时有警告", bool(warn) and "公网 IPv4" in warn, warn[:80])
        check("警告文案固定", PAGE_UNREACHABLE_MSG in warn or "无法显示构建的网页" in warn)
        set_setting("feishu_receive_mode", MODE_CALLBACK, user_id=0)
    st_api = client.get("/settings/api/feishu-ws-status")
    body = st_api.get_json() or {}
    check("状态 API ok", st_api.status_code == 200 and body.get("ok") and "mode" in (body.get("data") or {}),
          str(body)[:160])

    with app.test_request_context("/"):
        login_user(user)
        set_setting("feishu_receive_mode", MODE_CALLBACK, user_id=0)
        set_setting("feishu_receive_mode", MODE_CALLBACK, user_id=user.id)

print(f"\n通过 {len(PASSED)}  失败 {len(FAILED)}")
if FAILED:
    print("失败项：", ", ".join(FAILED))
    sys.exit(1)
