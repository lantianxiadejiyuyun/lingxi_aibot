"""设置页：账号（密码+时区）/ AI 设置（只读）/ 通知渠道 / 简报设置（PRG）。"""
from __future__ import annotations

from flask import (
    Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import PasswordField, SelectField
from wtforms.validators import DataRequired, EqualTo, Length

from app.extensions import csrf, db
from app.models.scheduled_job import (
    ACTION_EVENING_REVIEW, ACTION_MORNING_BRIEFING, ACTION_NOON_BRIEFING, ScheduledJob,
)
from app.services.notify_service import SCENE_KEYS, default_channels
from app.services.settings_service import get_setting_from, set_setting

bp = Blueprint("settings_page", __name__, url_prefix="/settings")

_TIMEZONE_CHOICES = [
    ("Asia/Shanghai", "上海 Asia/Shanghai"),
    ("Asia/Hong_Kong", "香港 Asia/Hong_Kong"),
    ("Asia/Tokyo", "东京 Asia/Tokyo"),
    ("Asia/Seoul", "首尔 Asia/Seoul"),
    ("UTC", "UTC 协调世界时"),
    ("Europe/London", "伦敦 Europe/London"),
    ("Europe/Berlin", "柏林 Europe/Berlin"),
    ("America/New_York", "纽约 America/New_York"),
    ("America/Los_Angeles", "洛杉矶 America/Los_Angeles"),
]

# 账号表单字段名 → 中文标签（校验失败 flash 用）
_ACCOUNT_FIELD_LABELS = {
    "old_password": "当前密码",
    "new_password": "新密码",
    "confirm": "确认新密码",
    "timezone": "时区",
}

# 默认推送渠道可选项
_CHANNEL_OPTIONS = (
    ("inapp", "站内"), ("serverchan", "Server酱"),
    ("feishu", "飞书"), ("feishu_app", "飞书机器人(应用)"),
)


def _ai_view() -> dict:
    """AI 配置视图：运行时值（DB 设置 > .env 回退），Key 只展示尾部。"""
    base_url = str(get_setting_from("llm_base_url", "LLM_BASE_URL", "https://api.deepseek.com/v1") or "")
    model = str(get_setting_from("llm_model", "LLM_MODEL", "deepseek-chat") or "")
    key = str(get_setting_from("llm_api_key", "LLM_API_KEY", "") or "")
    return {
        "base_url": base_url,
        "model": model,
        "api_key_configured": bool(key),
        "api_key_tail": key[-4:] if len(key) >= 4 else (key or "****"),
    }


class AccountForm(FlaskForm):
    old_password = PasswordField("当前密码", validators=[DataRequired(), Length(max=128)])
    new_password = PasswordField("新密码", validators=[DataRequired(), Length(min=6, max=128)])
    confirm = PasswordField("确认新密码", validators=[
        DataRequired(),
        EqualTo("new_password", message="两次输入的新密码不一致"),
    ])
    timezone = SelectField("时区", choices=_TIMEZONE_CHOICES, validators=[DataRequired()])


@bp.route("/")
@login_required
def index():
    form = AccountForm()
    form.timezone.data = current_user.timezone

    ai = _ai_view()
    channels = {
        "sc_key": get_setting_from("sc_key", "SC_KEY", ""),
        "feishu_webhook_url": get_setting_from("feishu_webhook_url", "FEISHU_WEBHOOK_URL", ""),
        "feishu_secret": get_setting_from("feishu_secret", "FEISHU_SECRET", ""),
        "default_channels": default_channels(),
    }
    scene_cfg = {}
    for scene, key in SCENE_KEYS.items():
        val = get_setting_from(key, None, None) or []
        scene_cfg[scene] = val[0] if isinstance(val, list) and val else (val or "")
    briefing = {
        "morning": get_setting_from("briefing_time_morning", None, "07:00"),
        "noon": get_setting_from("briefing_time_noon", None, "12:00"),
        "evening": get_setting_from("briefing_time_evening", None, "21:00"),
    }
    feishu_app = {
        "app_id": get_setting_from("feishu_app_id", "FEISHU_APP_ID", ""),
        "app_secret": get_setting_from("feishu_app_secret", "FEISHU_APP_SECRET", ""),
        "event_token": get_setting_from("feishu_event_token", "FEISHU_EVENT_TOKEN", ""),
        "encrypt_key": get_setting_from("feishu_event_encrypt_key", "FEISHU_EVENT_ENCRYPT_KEY", ""),
        "target": get_setting_from("feishu_app_target", "", ""),
        "target_type": get_setting_from("feishu_app_target_type", "", "chat_id"),
        "callback_url": request.host_url.rstrip("/") + url_for("feishu.event"),
        "configured": bool(get_setting_from("feishu_app_id", "FEISHU_APP_ID", "")
                           and get_setting_from("feishu_app_secret", "FEISHU_APP_SECRET", "")),
    }
    from app.services.backup_service import list_backups

    backup = {
        "enabled": bool(get_setting_from("backup_enabled", None, True)),
        "time": get_setting_from("backup_time", None, "03:00"),
        "keep": get_setting_from("backup_keep", None, 7),
        "channel": get_setting_from("backup_channel", None, "inapp"),
        "files": list_backups(),
    }
    site_pages = {
        "admin_domain": str(get_setting_from("admin_domain", "ADMIN_DOMAIN", "") or ""),
        "page_domain": str(get_setting_from("page_domain", "PAGE_DOMAIN", "") or ""),
    }
    tts_key = str(get_setting_from("tts_api_key", "TTS_API_KEY", "") or "")
    voice = {
        "base_url": str(get_setting_from("tts_base_url", "TTS_BASE_URL", "") or ""),
        "model": str(get_setting_from("tts_model", "TTS_MODEL", "") or ""),
        "voice": str(get_setting_from("tts_voice", "TTS_VOICE", "alloy") or "alloy"),
        "api_key_configured": bool(tts_key),
        "api_key_tail": tts_key[-4:] if len(tts_key) >= 4 else (tts_key or "****"),
    }
    search_cfg = {
        "provider": str(get_setting_from("search_provider", "SEARCH_PROVIDER", "bing") or "bing"),
        "searxng_base_url": str(get_setting_from("searxng_base_url", "SEARXNG_BASE_URL", "") or ""),
        "serper_key": str(get_setting_from("serper_api_key", "SERPER_API_KEY", "") or ""),
        "tavily_key": str(get_setting_from("tavily_api_key", "TAVILY_API_KEY", "") or ""),
    }
    img_key = str(get_setting_from("image_api_key", "IMAGE_API_KEY", "") or "")
    image = {
        "base_url": str(get_setting_from("image_base_url", "IMAGE_BASE_URL", "") or ""),
        "model": str(get_setting_from("image_model", "IMAGE_MODEL", "") or ""),
        "size": str(get_setting_from("image_size", "IMAGE_SIZE", "1024x1024") or ""),
        "api_key_configured": bool(img_key),
        "api_key_tail": img_key[-4:] if len(img_key) >= 4 else (img_key or "****"),
    }
    vision_key = str(get_setting_from("vision_api_key", "VISION_API_KEY", "") or "")
    vision = {
        "base_url": str(get_setting_from("vision_base_url", "VISION_BASE_URL", "") or ""),
        "model": str(get_setting_from("vision_model", "VISION_MODEL", "") or ""),
        "api_key_configured": bool(vision_key),
        "api_key_tail": vision_key[-4:] if len(vision_key) >= 4 else (vision_key or "****"),
    }
    return render_template(
        "settings/index.html",
        form=form, ai=ai, channels=channels, briefing=briefing,
        feishu_app=feishu_app, backup=backup, site_pages=site_pages, image=image,
        voice=voice, search_cfg=search_cfg, scene_cfg=scene_cfg, vision=vision,
        channel_options=_CHANNEL_OPTIONS,
    )


@bp.route("/account", methods=["POST"])
@login_required
def account():
    """修改密码 + 保存时区（校验旧密码，成功后一并保存）。"""
    form = AccountForm()
    if not form.validate_on_submit():
        for field, errs in form.errors.items():
            for err in errs:
                flash(f"{_ACCOUNT_FIELD_LABELS.get(field, field)}：{err}", "error")
        return redirect(url_for("settings_page.index"))
    if not current_user.check_password(form.old_password.data):
        flash("当前密码错误", "error")
        return redirect(url_for("settings_page.index"))
    current_user.set_password(form.new_password.data)
    current_user.timezone = form.timezone.data
    db.session.commit()
    flash("已保存", "success")
    return redirect(url_for("settings_page.index"))


@bp.route("/channels", methods=["POST"])
@login_required
def channels():
    """保存通知渠道配置（sc_key / feishu_webhook_url / feishu_secret / default_channels / 各场景渠道）。"""
    set_setting("sc_key", (request.form.get("sc_key") or "").strip())
    set_setting("feishu_webhook_url", (request.form.get("feishu_webhook_url") or "").strip())
    set_setting("feishu_secret", (request.form.get("feishu_secret") or "").strip())
    defaults = [c for c in request.form.getlist("default_channels")
                if c in dict(_CHANNEL_OPTIONS)]
    set_setting("default_channels", defaults or ["inapp"])
    # 各应用场景渠道（空 = 跟随全局默认）
    for scene, key in SCENE_KEYS.items():
        v = (request.form.get(f"scene_{scene}") or "").strip()
        set_setting(key, [v] if v else [])
    flash("已保存", "success")
    return redirect(url_for("settings_page.index"))


@bp.route("/ai", methods=["POST"])
@login_required
def ai():
    """保存 AI 模型配置（BaseURL / Model / API Key），保存后立即生效，无需重启。"""
    base_url = (request.form.get("llm_base_url") or "").strip().rstrip("/")
    model = (request.form.get("llm_model") or "").strip()
    api_key = (request.form.get("llm_api_key") or "").strip()
    clear_key = bool(request.form.get("clear_api_key"))

    if not base_url.startswith(("http://", "https://")):
        flash("Base URL 必须以 http:// 或 https:// 开头", "error")
        return redirect(url_for("settings_page.index"))
    if not model:
        flash("模型名不能为空", "error")
        return redirect(url_for("settings_page.index"))

    set_setting("llm_base_url", base_url)
    set_setting("llm_model", model)
    if clear_key:
        set_setting("llm_api_key", "")
    elif api_key:
        set_setting("llm_api_key", api_key)
    flash("AI 配置已保存，立即生效", "success")
    return redirect(url_for("settings_page.index"))


@bp.route("/feishu-app", methods=["POST"])
@login_required
def feishu_app():
    """保存飞书应用机器人配置（app_id/app_secret/事件订阅/默认发送目标）。"""
    set_setting("feishu_app_id", (request.form.get("feishu_app_id") or "").strip())
    set_setting("feishu_app_secret", (request.form.get("feishu_app_secret") or "").strip())
    set_setting("feishu_event_token", (request.form.get("feishu_event_token") or "").strip())
    set_setting("feishu_event_encrypt_key",
                (request.form.get("feishu_event_encrypt_key") or "").strip())
    target = (request.form.get("feishu_app_target") or "").strip()
    target_type = (request.form.get("feishu_app_target_type") or "chat_id").strip()
    if target_type not in ("chat_id", "open_id"):
        target_type = "chat_id"
    set_setting("feishu_app_target", target)
    set_setting("feishu_app_target_type", target_type)
    flash("飞书应用机器人配置已保存", "success")
    return redirect(url_for("settings_page.index"))


@bp.route("/backup", methods=["POST"])
@login_required
def backup():
    """保存数据库备份配置，并同步内置备份任务（时间/开关）。"""
    enabled = bool(request.form.get("backup_enabled"))
    time_str = (request.form.get("backup_time") or "").strip()
    if not _valid_time(time_str):
        flash("备份时间格式不正确，请使用 HH:MM", "error")
        return redirect(url_for("settings_page.index"))
    try:
        keep = int(request.form.get("backup_keep") or 7)
        keep = max(1, min(30, keep))
    except (TypeError, ValueError):
        keep = 7
    channel = (request.form.get("backup_channel") or "inapp").strip()

    set_setting("backup_enabled", enabled)
    set_setting("backup_time", time_str)
    set_setting("backup_keep", keep)
    set_setting("backup_channel", channel)

    # 同步内置任务：cron = '分 时 * * *'，开关同步
    from app.models.scheduled_job import ACTION_DATA_BACKUP, ScheduledJob

    job = ScheduledJob.query.filter_by(job_key=ACTION_DATA_BACKUP).first()
    if job is not None:
        hour, minute = time_str.split(":")
        job.cron = f"{int(minute)} {int(hour)} * * *"
        job.enabled = enabled
        db.session.commit()
        if current_app.scheduler:
            current_app.scheduler.reschedule()
    flash("备份配置已保存", "success")
    return redirect(url_for("settings_page.index"))


@bp.route("/backups/download/<path:name>")
@login_required
def backup_download(name):
    """下载备份文件（校验文件名，仅限备份目录内 backup-*.json）。"""
    from pathlib import Path

    from flask import send_file

    backup_dir = Path(current_app.config["BACKUP_DIR"])
    p = (backup_dir / name).resolve()
    if (p.parent != backup_dir.resolve()
            or not p.name.startswith("backup-")
            or not (p.name.endswith(".json") or p.name.endswith(".json.gz"))
            or not p.exists()):
        flash("备份文件不存在", "error")
        return redirect(url_for("settings_page.index"))
    return send_file(p, as_attachment=True, download_name=p.name)


@bp.route("/backups/delete/<path:name>", methods=["POST"])
@login_required
def backup_delete(name):
    """删除一个备份文件（POST + CSRF）。"""
    from app.services.backup_service import delete_backup

    try:
        delete_backup(name)
        flash(f"已删除备份：{name}", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("settings_page.index"))


@bp.route("/export")
@login_required
def export_data():
    """一键导出：生成最新备份 JSON 并直接下载到本地。"""
    from pathlib import Path

    from flask import send_file

    from app.services.backup_service import backup_to_json

    try:
        path = backup_to_json()
    except Exception:  # noqa: BLE001
        current_app.logger.exception("数据导出失败")
        flash("导出失败，请稍后重试", "error")
        return redirect(url_for("settings_page.index"))
    return send_file(path, as_attachment=True, download_name=path.name)


@bp.route("/reset", methods=["POST"])
@login_required
def reset_data():
    """一键重置：清空业务数据（保留账号与系统配置），重置前自动备份。"""
    from app.services.backup_service import backup_to_json
    from app.services.data_service import reset_all_data

    password = request.form.get("password") or ""
    confirm = (request.form.get("confirm_word") or "").strip()

    if confirm != "重置" or not current_user.check_password(password):
        flash("重置已取消：密码错误，或未正确输入确认文字「重置」", "error")
        return redirect(url_for("settings_page.index"))

    # 重置前先做一次安全备份，备份失败则中止（绝不在无备份的情况下清空数据）
    try:
        backup_path = backup_to_json()
    except Exception:  # noqa: BLE001
        current_app.logger.exception("重置前自动备份失败")
        flash("重置前自动备份失败，已中止重置（请检查 data/backups 目录权限）", "error")
        return redirect(url_for("settings_page.index"))

    try:
        deleted = reset_all_data()
    except Exception:  # noqa: BLE001
        current_app.logger.exception("重置数据失败")
        flash("重置失败，已回滚（数据未受影响）", "error")
        return redirect(url_for("settings_page.index"))

    total = sum(deleted.values())
    flash(f"数据已重置：共清理 {total} 条记录，重置前备份已保存为 {backup_path.name}", "success")
    return redirect(url_for("settings_page.index"))


@bp.route("/restore", methods=["POST"])
@login_required
def restore_backup():
    """从备份恢复数据：可上传备份文件，或选择服务器已有备份。"""
    import os
    import tempfile
    from pathlib import Path

    from app.services.backup_service import backup_to_json, restore_from_json

    password = request.form.get("password") or ""
    confirm = (request.form.get("confirm_word") or "").strip()
    if confirm != "恢复" or not current_user.check_password(password):
        flash("恢复已取消：密码错误，或未正确输入确认文字「恢复」", "error")
        return redirect(url_for("settings_page.index"))

    # 1) 定位备份来源：上传文件优先，否则用服务器已有备份
    upload = request.files.get("file")
    tmp_path: Path | None = None
    if upload and upload.filename:
        if not (upload.filename.endswith(".json") or upload.filename.endswith(".json.gz")):
            flash("仅支持 .json 或 .json.gz 备份文件", "error")
            return redirect(url_for("settings_page.index"))
        suffix = ".json.gz" if upload.filename.endswith(".json.gz") else ".json"
        fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="restore-")
        os.close(fd)
        upload.save(tmp)
        tmp_path = Path(tmp)
        source_path = tmp_path
    else:
        name = (request.form.get("backup_name") or "").strip()
        backup_dir = Path(current_app.config["BACKUP_DIR"])
        p = (backup_dir / name).resolve() if name else None
        if p is None or p.parent != backup_dir.resolve() or not p.exists():
            flash("请选择要恢复的备份，或上传一个备份文件", "error")
            return redirect(url_for("settings_page.index"))
        source_path = p

    # 2) 恢复前先做一次当前数据的安全备份
    try:
        safety = backup_to_json()
    except Exception:  # noqa: BLE001
        current_app.logger.exception("恢复前自动备份失败")
        if tmp_path:
            tmp_path.unlink(missing_ok=True)
        flash("恢复前自动备份失败，已中止恢复（请检查 data/backups 目录权限）", "error")
        return redirect(url_for("settings_page.index"))

    # 3) 执行恢复
    try:
        restored = restore_from_json(source_path)
    except Exception as e:  # noqa: BLE001
        current_app.logger.exception("恢复数据失败")
        if tmp_path:
            tmp_path.unlink(missing_ok=True)
        flash(f"恢复失败，已回滚（数据未受影响）：{str(e)[:120]}", "error")
        return redirect(url_for("settings_page.index"))

    if tmp_path:
        tmp_path.unlink(missing_ok=True)

    total = sum(restored.values())
    flash(f"数据已恢复：共 {len(restored)} 张表、{total} 条记录，恢复前备份为 {safety.name}", "success")
    return redirect(url_for("settings_page.index"))


def _valid_time(t: str) -> bool:
    """校验 'HH:MM' 时间格式（0-23 时，0-59 分）。"""
    try:
        hour, minute = t.split(":")
        h, m = int(hour), int(minute)
        return 0 <= h <= 23 and 0 <= m <= 59
    except (TypeError, ValueError):
        return False


def _apply_briefing_job(action: str, t: str) -> None:
    """把简报时间同步到内置定时任务：cron = '分 时 * * *'。"""
    if not _valid_time(t):
        return
    hour, minute = t.split(":")  # "HH:MM" → 前为时、后为分
    cron = f"{int(minute)} {int(hour)} * * *"
    job = ScheduledJob.query.filter_by(job_key=action).first()
    if job is not None:
        job.cron = cron
        db.session.commit()


@bp.route("/briefing", methods=["POST"])
@login_required
def briefing():
    """保存早安简报 / 午间简报 / 晚间复盘时间，并同步内置定时任务后触发调度器重载。"""
    morning = (request.form.get("briefing_time_morning") or "").strip()
    noon = (request.form.get("briefing_time_noon") or "").strip()
    evening = (request.form.get("briefing_time_evening") or "").strip()
    if not _valid_time(morning) or not _valid_time(noon) or not _valid_time(evening):
        flash("简报时间格式不正确，请使用 HH:MM", "error")
        return redirect(url_for("settings_page.index"))
    set_setting("briefing_time_morning", morning)
    set_setting("briefing_time_noon", noon)
    set_setting("briefing_time_evening", evening)
    _apply_briefing_job(ACTION_MORNING_BRIEFING, morning)
    _apply_briefing_job(ACTION_NOON_BRIEFING, noon)
    _apply_briefing_job(ACTION_EVENING_REVIEW, evening)
    if current_app.scheduler:
        current_app.scheduler.reschedule()
    flash("已保存", "success")
    return redirect(url_for("settings_page.index"))


@bp.route("/pages-domain", methods=["POST"])
@login_required
def pages_domain():
    """保存域名设置（后台域名 + 网页域名），留空则回退当前主机路径模式。"""
    import re

    admin = (request.form.get("admin_domain") or "").strip().lower()
    page = (request.form.get("page_domain") or "").strip().lower()
    domain_re = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
    for name, value in (("后台域名", admin), ("网页域名", page)):
        if not value:
            continue
        value = re.sub(r"^https?://", "", value).split("/")[0].split(":")[0].strip().rstrip(".")
        if not re.match(domain_re, value):
            flash(f"{name}格式不正确（如 {name}.example.com，不要带协议/端口/路径）", "error")
            return redirect(url_for("settings_page.index"))
        if name == "后台域名":
            admin = value
        else:
            page = value
    if admin and page and admin == page:
        flash("后台域名与网页域名不能相同", "error")
        return redirect(url_for("settings_page.index"))
    set_setting("admin_domain", admin)
    set_setting("page_domain", page)
    flash("域名设置已保存，立即生效", "success")
    return redirect(url_for("settings_page.index"))


@bp.route("/image", methods=["POST"])
@login_required
def image():
    """保存图片生成配置（OpenAI 兼容 images 接口），保存后立即生效。"""
    import re

    base_url = (request.form.get("image_base_url") or "").strip().rstrip("/")
    model = (request.form.get("image_model") or "").strip()
    size = (request.form.get("image_size") or "1024x1024").strip()
    api_key = (request.form.get("image_api_key") or "").strip()
    clear_key = bool(request.form.get("clear_image_key"))

    if base_url and not base_url.startswith(("http://", "https://")):
        flash("Base URL 必须以 http:// 或 https:// 开头", "error")
        return redirect(url_for("settings_page.index"))
    if size and not re.match(r"^\d{2,4}x\d{2,4}$", size):
        flash("尺寸格式不正确，应为 宽x高（如 1024x1024）", "error")
        return redirect(url_for("settings_page.index"))

    set_setting("image_base_url", base_url)
    set_setting("image_model", model)
    set_setting("image_size", size or "1024x1024")
    if clear_key:
        set_setting("image_api_key", "")
    elif api_key:
        set_setting("image_api_key", api_key)
    flash("图片生成配置已保存，立即生效", "success")
    return redirect(url_for("settings_page.index"))


@bp.route("/voice", methods=["POST"])
@login_required
def voice():
    """保存语音（TTS）配置；留空则前端用浏览器自带朗读。"""
    base_url = (request.form.get("tts_base_url") or "").strip().rstrip("/")
    model = (request.form.get("tts_model") or "").strip()
    voice_name = (request.form.get("tts_voice") or "alloy").strip()
    api_key = (request.form.get("tts_api_key") or "").strip()
    clear_key = bool(request.form.get("clear_tts_key"))
    if base_url and not base_url.startswith(("http://", "https://")):
        flash("Base URL 必须以 http:// 或 https:// 开头", "error")
        return redirect(url_for("settings_page.index"))
    set_setting("tts_base_url", base_url)
    set_setting("tts_model", model)
    set_setting("tts_voice", voice_name or "alloy")
    if clear_key:
        set_setting("tts_api_key", "")
    elif api_key:
        set_setting("tts_api_key", api_key)
    flash("语音配置已保存，立即生效", "success")
    return redirect(url_for("settings_page.index"))


@bp.route("/search", methods=["POST"])
@login_required
def search():
    """保存联网搜索配置（提供方 + 相应密钥），保存后立即生效。"""
    provider = (request.form.get("search_provider") or "bing").strip().lower()
    if provider not in ("bing", "duckduckgo", "searxng", "serper", "tavily"):
        flash("未知的搜索提供方", "error")
        return redirect(url_for("settings_page.index"))
    searxng = (request.form.get("searxng_base_url") or "").strip().rstrip("/")
    if searxng and not searxng.startswith(("http://", "https://")):
        flash("SearXNG 地址必须以 http:// 或 https:// 开头", "error")
        return redirect(url_for("settings_page.index"))
    set_setting("search_provider", provider)
    set_setting("searxng_base_url", searxng)
    set_setting("serper_api_key", (request.form.get("serper_api_key") or "").strip())
    set_setting("tavily_api_key", (request.form.get("tavily_api_key") or "").strip())
    flash("联网搜索配置已保存，立即生效", "success")
    return redirect(url_for("settings_page.index"))


@bp.route("/vision", methods=["POST"])
@login_required
def vision():
    """保存视觉（多模态识图）模型配置，保存后立即生效。"""
    base_url = (request.form.get("vision_base_url") or "").strip().rstrip("/")
    model = (request.form.get("vision_model") or "").strip()
    api_key = (request.form.get("vision_api_key") or "").strip()
    clear_key = bool(request.form.get("clear_vision_key"))

    if base_url and not base_url.startswith(("http://", "https://")):
        flash("Base URL 必须以 http:// 或 https:// 开头", "error")
        return redirect(url_for("settings_page.index"))
    if not model:
        flash("视觉模型名不能为空", "error")
        return redirect(url_for("settings_page.index"))

    set_setting("vision_base_url", base_url)
    set_setting("vision_model", model)
    if clear_key:
        set_setting("vision_api_key", "")
    elif api_key:
        set_setting("vision_api_key", api_key)
    flash("视觉模型配置已保存，立即生效", "success")
    return redirect(url_for("settings_page.index"))
