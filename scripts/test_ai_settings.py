"""验证 AI 配置网页修改 + 运行时生效。"""
import re
import sys
import time

import requests

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


for _ in range(30):
    try:
        if s.get(BASE + "/login", timeout=2).status_code == 200:
            break
    except requests.RequestException:
        time.sleep(1)

r = s.get(BASE + "/login")
token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.text).group(1)
s.post(BASE + "/login", data={"csrf_token": token, "username": "admin", "password": "admin123"})
s.headers["X-CSRFToken"] = token

# 1. 设置页显示可编辑表单（env 默认值回填）
r = s.get(BASE + "/settings")
check("AI 表单含 BaseURL 默认值", 'value="https://api.deepseek.com/v1"' in r.text)
check("AI 表单含模型默认值", 'value="deepseek-chat"' in r.text)
check("AI 表单含保存按钮", "保存 AI 配置" in r.text and "测试连接" in r.text)

# 2. 保存新配置（网页修改）
r = s.post(BASE + "/settings/ai", data={
    "csrf_token": token, "llm_base_url": "https://api.deepseek.com/v1/",
    "llm_model": "deepseek-chat", "llm_api_key": "sk-test-fake-key-1234",
}, allow_redirects=False)
check("保存 AI 配置 302", r.status_code == 302)
r = s.get(BASE + "/settings")
check("保存后 Key 显示已配置", "已配置（结尾" in r.text and "1234" in r.text)
check("保存后 BaseURL 去除尾部斜杠", 'value="https://api.deepseek.com/v1"' in r.text)

# 3. 测试连接（假 Key → 应返回连接错误而非“未配置”，证明运行时读到新 Key）
r = s.post(BASE + "/settings/api/test-llm", json={})
body = r.json()
check("test-llm 读到新 Key（报连接错误而非未配置）",
      body.get("ok") is False and "尚未配置" not in body.get("error", ""),
      str(body)[:160])
check("test-llm 返回 LLM 错误信息", bool(body.get("error")), str(body)[:160])

# 4. 清空 Key（勾选清除）→ 恢复未配置状态
r = s.post(BASE + "/settings/ai", data={
    "csrf_token": token, "llm_base_url": "https://api.deepseek.com/v1",
    "llm_model": "deepseek-chat", "clear_api_key": "1",
}, allow_redirects=False)
check("清除 Key 302", r.status_code == 302)
r = s.get(BASE + "/settings")
check("清除后显示未配置", "未配置" in r.text and "已配置（结尾" not in r.text)
r = s.post(BASE + "/settings/api/test-llm", json={})
check("test-llm 未配置提示", r.json().get("ok") is False and "尚未配置 API Key" in r.json().get("error", ""))

# 5. 校验非法输入
r = s.post(BASE + "/settings/ai", data={
    "csrf_token": token, "llm_base_url": "ftp://bad", "llm_model": "deepseek-chat",
}, allow_redirects=False)
check("非法 BaseURL 302 拒绝", r.status_code == 302)
r = s.get(BASE + "/settings")
check("非法 BaseURL 未被保存", 'value="ftp://bad"' not in r.text)

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
sys.exit(1 if FAILED else 0)
