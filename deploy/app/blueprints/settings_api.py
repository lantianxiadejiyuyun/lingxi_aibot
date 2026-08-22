"""设置 API（测试类）路由：从 settings_page 拆分，统一挂在 /settings/api 下。"""
from __future__ import annotations

import requests
from flask import Blueprint, jsonify, request
from flask_login import login_required

from app.blueprints.settings_page import _CHANNEL_OPTIONS
from app.extensions import csrf
from app.services.notify_service import notify

api_bp = Blueprint("settings_api", __name__, url_prefix="/settings/api")


@api_bp.route("/test-llm", methods=["POST"])
@csrf.exempt
@login_required
def test_llm():
    """测试 LLM 连接：用当前配置发起一次最小对话（JSON 接口）。"""
    from app.ai.llm import LLMClient, LLMError

    llm = LLMClient()
    if not llm.is_configured:
        return jsonify({"ok": False, "error": "尚未配置 API Key"}), 400
    try:
        content, _ = llm.chat([
            {"role": "user", "content": "请只回复四个字：连接正常"},
        ])
        return jsonify({"ok": True, "data": {"reply": content[:80], "model": llm.model}})
    except LLMError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@api_bp.route("/test-feishu-app", methods=["POST"])
@csrf.exempt
@login_required
def test_feishu_app():
    """通过应用机器人 API 发送一条测试消息（JSON 接口）。"""
    from app.services.channels.feishu_app import FeishuAppChannel

    ch = FeishuAppChannel()
    try:
        if not ch.configured:
            return jsonify({"ok": False, "error": "尚未配置 App ID / App Secret"}), 400
        ch.send("🧪 测试通知", "这是一条来自 灵犀 的飞书应用机器人测试消息")
        return jsonify({"ok": True, "data": {"status": "sent"}})
    except Exception as e:  # noqa: BLE001 —— 错误信息回给前端
        return jsonify({"ok": False, "error": str(e)[:300]}), 502


@api_bp.route("/backup-now", methods=["POST"])
@csrf.exempt
@login_required
def backup_now():
    """立即执行一次数据库备份（JSON 接口）。"""
    from app.services.backup_service import backup_to_json

    try:
        path = backup_to_json()
        size = path.stat().st_size
        return jsonify({"ok": True, "data": {"file": path.name, "size": size}})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)[:300]}), 500


@api_bp.route("/test-image", methods=["POST"])
@csrf.exempt
@login_required
def test_image():
    """测试图片生成：用当前配置生成一张小图（消耗一次配额，JSON 接口）。"""
    from app.services import image_service

    if not image_service.is_configured():
        return jsonify({"ok": False, "error": "尚未配置图片生成（BaseURL / API Key / 模型）"}), 400
    try:
        asset = image_service.generate(
            "一只可爱的卡通猫，简约风格，纯色背景，测试图片", size="512x512")
        return jsonify({"ok": True, "data": {"url": image_service.asset_url(asset), "id": asset.id}})
    except image_service.ImageError as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@api_bp.route("/test-tts", methods=["POST"])
@csrf.exempt
@login_required
def test_tts():
    """测试 TTS：合成一段短音频（JSON 接口返回字节数）。"""
    from app.services.settings_service import get_setting_from

    base = str(get_setting_from("tts_base_url", "TTS_BASE_URL", "") or "").strip().rstrip("/")
    key = str(get_setting_from("tts_api_key", "TTS_API_KEY", "") or "").strip()
    model = str(get_setting_from("tts_model", "TTS_MODEL", "") or "").strip()
    if not (base and key and model):
        return jsonify({"ok": False, "error": "尚未配置 TTS（BaseURL / API Key / 模型）"}), 400
    voice = str(get_setting_from("tts_voice", "TTS_VOICE", "alloy") or "alloy").strip()
    try:
        resp = requests.post(
            f"{base}/audio/speech",
            json={"model": model, "input": "你好，我是 灵犀，语音功能测试正常。",
                  "voice": voice, "response_format": "mp3"},
            headers={"Authorization": f"Bearer {key}"}, timeout=60,
        )
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 502
    if resp.status_code != 200:
        return jsonify({"ok": False, "error": f"TTS 失败 HTTP {resp.status_code}: {resp.text[:150]}"}), 502
    return jsonify({"ok": True, "data": {"bytes": len(resp.content)}})


@api_bp.route("/test-search", methods=["POST"])
@csrf.exempt
@login_required
def test_search():
    """测试联网搜索：用当前配置搜索一次（JSON 接口）。"""
    from app.services import web_search_service

    if not web_search_service.is_configured():
        return jsonify({"ok": False, "error": "当前提供方未配置（DuckDuckGo 免 key 除外）"}), 400
    try:
        results = web_search_service.search("灵犀 个人助理", limit=3)
    except web_search_service.SearchError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True, "data": {
        "count": len(results),
        "first": results[0] if results else None,
    }})


@api_bp.route("/test-channel", methods=["POST"])
@csrf.exempt
@login_required
def test_channel():
    """发送测试通知（JSON 接口，前端 toast 展示结果）。"""
    data = request.get_json(silent=True) or {}
    channel = (data.get("channel") or "").strip()
    if channel not in dict(_CHANNEL_OPTIONS):
        return jsonify({"ok": False, "error": "未知渠道"}), 400
    records = notify("🧪 测试通知", "这是一条来自 灵犀 的测试消息", [channel])
    rec = records[0]
    return jsonify({"ok": True, "data": {"status": rec.status, "error": rec.error}})
