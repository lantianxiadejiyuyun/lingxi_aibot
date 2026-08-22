"""LLM：OpenAI / Anthropic 协议转换、每用户独立 API Key。"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run import app
from app.ai.llm import (
    LLMClient, PROVIDER_PRESETS, anthropic_messages_url, default_for_protocol,
    grouped_provider_presets, iter_anthropic_sse, normalize_protocol,
    openai_tools_to_anthropic, parse_anthropic_content, to_anthropic_payload,
)
from app.extensions import db
from app.models.setting import Setting
from app.models.user import User
from app.services.install_service import ensure_schema
from app.services.settings_service import get_own_setting, set_setting
from app.utils.scoping import set_current_user_id, clear_current_user_id

PASSED, FAILED = [], []
MARK = "[test-llm-proto]"


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


# ---------- 纯函数（无需数据库）----------
check("normalize openai", normalize_protocol("OpenAI") == "openai")
check("normalize claude→anthropic", normalize_protocol("claude") == "anthropic")
check("normalize messages→anthropic", normalize_protocol("messages") == "anthropic")
check("normalize 空值默认 openai", normalize_protocol("") == "openai")

ids = [p["id"] for p in PROVIDER_PRESETS]
check("预设 id 不重复", len(ids) == len(set(ids)), str(ids))
check("每条预设有 http(s) 地址",
      all(str(p.get("base_url") or "").startswith(("http://", "https://")) for p in PROVIDER_PRESETS))
check("每条预设有模型名", all(bool(p.get("model")) for p in PROVIDER_PRESETS))
check("含国内厂商",
      {"deepseek", "qwen", "kimi", "glm", "doubao", "siliconflow"} <= set(ids))
check("含国际厂商", {"openai", "anthropic", "xai", "gemini", "openrouter"} <= set(ids))
check("含本地 Ollama/LM Studio", {"ollama", "lmstudio"} <= set(ids))
check("智谱地址是 paas/v4",
      next(p["base_url"] for p in PROVIDER_PRESETS if p["id"] == "glm").endswith("/paas/v4"))
check("Anthropic 仍走 messages 协议",
      next(p["protocol"] for p in PROVIDER_PRESETS if p["id"] == "anthropic") == "anthropic")
gnames = [g for g, _ in grouped_provider_presets()]
check("预设分成国内/国际/本地", gnames == ["国内", "国际", "本地"])
check("分组条目数等于预设总数",
      sum(len(ps) for _, ps in grouped_provider_presets()) == len(PROVIDER_PRESETS))

d_oa = default_for_protocol("openai")
check("openai 缺省 DeepSeek 地址", "deepseek.com" in d_oa["base_url"])
d_an = default_for_protocol("anthropic")
check("anthropic 缺省官方地址", d_an["base_url"] == "https://api.anthropic.com")
check("anthropic 缺省模型含 claude", "claude" in d_an["model"])

check("url 官方根路径",
      anthropic_messages_url("https://api.anthropic.com") == "https://api.anthropic.com/v1/messages")
check("url 已带 /v1",
      anthropic_messages_url("https://api.anthropic.com/v1") == "https://api.anthropic.com/v1/messages")
check("url 已是 messages",
      anthropic_messages_url("https://gw.example/v1/messages") == "https://gw.example/v1/messages")

tools = openai_tools_to_anthropic([
    {"type": "function", "function": {
        "name": "create_note", "description": "记笔记",
        "parameters": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
    }},
])
check("工具转 Anthropic name", tools and tools[0]["name"] == "create_note")
check("工具转 input_schema", tools[0]["input_schema"]["required"] == ["title"])

payload = to_anthropic_payload([
    {"role": "system", "content": "你是灵犀"},
    {"role": "user", "content": "记一下开会"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "create_note", "arguments": '{"title":"开会"}'}},
    ]},
    {"role": "tool", "tool_call_id": "call_1", "content": '{"ok":true}'},
], tools=[{"type": "function", "function": {"name": "create_note", "description": "记", "parameters": {"type": "object"}}}])
check("system 抽出为顶层", payload.get("system") == "你是灵犀")
check("首条是 user", payload["messages"][0]["role"] == "user")
check("assistant 含 tool_use",
      payload["messages"][1]["role"] == "assistant"
      and any(b.get("type") == "tool_use" and b.get("id") == "call_1" for b in payload["messages"][1]["content"]))
check("tool 结果并入 user tool_result",
      payload["messages"][2]["role"] == "user"
      and payload["messages"][2]["content"][0]["type"] == "tool_result"
      and payload["messages"][2]["content"][0]["tool_use_id"] == "call_1")
check("payload 含 tools", payload.get("tools") and payload["tools"][0]["name"] == "create_note")

merged = to_anthropic_payload([
    {"role": "user", "content": "a"},
    {"role": "assistant", "content": "b", "tool_calls": [
        {"id": "t1", "function": {"name": "x", "arguments": "{}"}},
        {"id": "t2", "function": {"name": "y", "arguments": "{}"}},
    ]},
    {"role": "tool", "tool_call_id": "t1", "content": "r1"},
    {"role": "tool", "tool_call_id": "t2", "content": "r2"},
])
check("连续 tool 合并为一条 user",
      len(merged["messages"]) == 3
      and merged["messages"][-1]["role"] == "user"
      and len(merged["messages"][-1]["content"]) == 2)

text, calls = parse_anthropic_content([
    {"type": "text", "text": "好的"},
    {"type": "tool_use", "id": "toolu_1", "name": "list_tasks", "input": {"status": "open"}},
])
check("解析文本", text == "好的")
check("解析 tool_use",
      len(calls) == 1 and calls[0]["name"] == "list_tasks"
      and json.loads(calls[0]["arguments"])["status"] == "open")

sse = iter_anthropic_sse([
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "你"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "好"}},
    {"type": "content_block_start", "index": 1,
     "content_block": {"type": "tool_use", "id": "toolu_x", "name": "list_notes", "input": {}}},
    {"type": "content_block_delta", "index": 1,
     "delta": {"type": "input_json_delta", "partial_json": "{\"q\":"}},
    {"type": "content_block_delta", "index": 1,
     "delta": {"type": "input_json_delta", "partial_json": "\"x\"}"}},
    {"type": "message_stop"},
])
deltas = [e["text"] for e in sse if e["type"] == "delta"]
tcs = [e for e in sse if e["type"] == "tool_calls"]
check("SSE 文本增量", deltas == ["你", "好"])
check("SSE 累积工具 JSON",
      tcs and tcs[0]["calls"][0]["name"] == "list_notes"
      and tcs[0]["calls"][0]["arguments"] == '{"q":"x"}')


# ---------- 每用户 Key 隔离（需要数据库）----------
with app.app_context():
    ensure_schema()
    leftover = User.query.filter(User.username.like(f"{MARK}%")).all()
    for u in leftover:
        Setting.query.filter_by(user_id=u.id).delete()
        db.session.delete(u)
    if leftover:
        db.session.commit()

    admin = User.query.filter_by(is_admin=True).order_by(User.id).first() \
        or User.query.order_by(User.id).first()
    check("存在管理员", admin is not None)
    if admin is None:
        print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
        sys.exit(1)

    other = User(username=f"{MARK}user", timezone="Asia/Shanghai", is_admin=False)
    other.set_password("test-llm-proto-pass")
    db.session.add(other)
    db.session.commit()

    backup = {
        "llm_protocol": get_own_setting("llm_protocol", None, user_id=admin.id),
        "llm_base_url": get_own_setting("llm_base_url", None, user_id=admin.id),
        "llm_model": get_own_setting("llm_model", None, user_id=admin.id),
        "llm_api_key": get_own_setting("llm_api_key", None, user_id=admin.id),
    }

    try:
        set_setting("llm_protocol", "openai", user_id=admin.id)
        set_setting("llm_base_url", "https://api.openai.com/v1", user_id=admin.id)
        set_setting("llm_model", "gpt-4o-mini", user_id=admin.id)
        set_setting("llm_api_key", "sk-admin-key-AAAA", user_id=admin.id)

        set_current_user_id(admin.id)
        llm_admin = LLMClient()
        cfg_admin = llm_admin._read_config()
        check("管理员读到自己的 Key", cfg_admin["api_key"] == "sk-admin-key-AAAA")
        check("管理员协议 openai", cfg_admin["protocol"] == "openai")
        check("管理员已配置", llm_admin.is_configured is True)

        set_setting("llm_api_key", "sk-SHOULD-NOT-LEAK", user_id=0)
        set_current_user_id(other.id)
        llm_other = LLMClient()
        cfg_other = llm_other._read_config()
        check("未配置用户没有 Key", cfg_other["api_key"] == "")
        check("未配置用户 is_configured False", llm_other.is_configured is False)
        check("get_own_setting 不回退全局",
              get_own_setting("llm_api_key", "", user_id=other.id) == "")
        check("旧的 get_setting_from 仍可能回退（故运行时不用它读 Key）", True)

        set_setting("llm_protocol", "anthropic", user_id=other.id)
        set_setting("llm_base_url", "https://api.anthropic.com", user_id=other.id)
        set_setting("llm_model", "claude-sonnet-4-5", user_id=other.id)
        set_setting("llm_api_key", "sk-ant-user-BBBB", user_id=other.id)
        cfg_other = LLMClient()._read_config()
        check("第二用户自己的 Anthropic Key", cfg_other["api_key"] == "sk-ant-user-BBBB")
        check("第二用户协议 anthropic", cfg_other["protocol"] == "anthropic")
        check("第二用户模型 claude", "claude" in cfg_other["model"])

        set_current_user_id(admin.id)
        cfg_admin2 = LLMClient()._read_config()
        check("管理员 Key 未被覆盖", cfg_admin2["api_key"] == "sk-admin-key-AAAA")
        check("管理员仍是 openai", cfg_admin2["protocol"] == "openai")
        clear_current_user_id()

        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(other.id)
            sess["_fresh"] = True
        r = client.get("/settings/")
        body = r.get_data(as_text=True)
        check("设置页含协议选项", "Anthropic（Claude）" in body and "OpenAI 兼容" in body)
        check("设置页含快捷预设", "DeepSeek" in body and "data-llm-preset" in body)
        check("设置页含国内厂商按钮", "通义千问" in body and "智谱 GLM" in body and "硅基流动" in body)
        check("设置页含国际/本机按钮", "xAI Grok" in body and "Ollama" in body)
        check("快捷按钮带官方地址",
              "https://api.deepseek.com/v1" in body
              and "https://dashscope.aliyuncs.com/compatible-mode/v1" in body)
        check("设置页强调每人自己的 Key", "自己的 API Key" in body)
        check("第二用户看到自己的 Key 尾", "BBBB" in body)
        check("第二用户看不到管理员 Key 尾", "AAAA" not in body)
        check("Anthropic 协议已勾选", 'value="anthropic" checked' in body)
        m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', body)
        check("设置页含 CSRF", bool(m))
        token = m.group(1) if m else ""

        r = client.post("/settings/ai", data={
            "csrf_token": token,
            "llm_protocol": "openai",
            "llm_base_url": "https://api.deepseek.com/v1",
            "llm_model": "deepseek-chat",
            "llm_api_key": "sk-user-CCCC",
        }, follow_redirects=False)
        check("保存协议 302", r.status_code in (302, 303))
        with app.app_context():
            check("保存后协议变 openai",
                  get_own_setting("llm_protocol", "", user_id=other.id) == "openai")
            check("保存后 Key 更新",
                  get_own_setting("llm_api_key", "", user_id=other.id) == "sk-user-CCCC")
            check("保存不影响管理员",
                  get_own_setting("llm_api_key", "", user_id=admin.id) == "sk-admin-key-AAAA")

        r = client.post("/settings/ai", data={
            "csrf_token": token,
            "llm_protocol": "openai",
            "llm_base_url": "ftp://bad",
            "llm_model": "x",
        }, follow_redirects=False)
        check("非法地址拒绝", r.status_code in (302, 303))
        with app.app_context():
            check("非法地址未写入",
                  get_own_setting("llm_base_url", "", user_id=other.id) != "ftp://bad")
    finally:
        clear_current_user_id()
        with app.app_context():
            for k, v in backup.items():
                if v is None:
                    Setting.query.filter_by(key=k, user_id=admin.id).delete()
                else:
                    set_setting(k, v, user_id=admin.id)
            Setting.query.filter_by(user_id=other.id).delete()
            Setting.query.filter_by(key="llm_api_key", user_id=0).update({"value": ""})
            u = db.session.get(User, other.id)
            if u is not None:
                db.session.delete(u)
            db.session.commit()

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
sys.exit(1 if FAILED else 0)
