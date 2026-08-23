"""测试公共工具：登录（自动处理登录限流 429 退避重试）。"""
import re
import time


def login(session, base="http://127.0.0.1:5000",
          username="admin", password="admin123"):
    """登录并返回 CSRF token；429 限流时退避重试（最多约 8 次尝试）。"""
    for attempt in range(8):
        try:
            r = session.get(base + "/login", timeout=5)
        except Exception:  # noqa: BLE001 —— 服务器可能刚重启
            time.sleep(3)
            continue
        if r.status_code == 429:  # GET 也可能被限流
            print("  (登录限流，等待 65s 后重试…)")
            time.sleep(65)
            continue
        m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.text)
        if not m:
            time.sleep(3)
            continue
        token = m.group(1)
        r = session.post(base + "/login",
                         data={"csrf_token": token, "username": username, "password": password},
                         allow_redirects=False)
        if r.status_code == 302:
            # 登录后刷新 CSRF（Flask-Login 可能轮换 session），并注入后续 JSON 请求头
            try:
                r2 = session.get(base + "/", timeout=5)
                m2 = re.search(r'name="csrf-token"[^>]*content="([^"]+)"', r2.text)
                if m2:
                    token = m2.group(1)
            except Exception:
                pass
            session.headers["X-CSRFToken"] = token
            return token
        if r.status_code == 429:
            print("  (登录限流，等待 65s 后重试…)")
            time.sleep(65)
            continue
        time.sleep(3)
    return None
