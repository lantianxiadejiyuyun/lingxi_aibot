"""设置页：账号（密码+时区）/ AI 设置（只读）/ 通知渠道 / 简报设置（PRG）。"""
from __future__ import annotations

from flask import (
    Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import PasswordField, SelectField
from wtforms.validators import DataRequired, EqualTo, Length

from app.extensions import db
from app.models.scheduled_job import (
    ACTION_EVENING_REVIEW, ACTION_MORNING_BRIEFING, ACTION_NOON_BRIEFING, ScheduledJob,
)
from app.services.notify_service import (
    GROUP_PREFIX, SCENE_KEYS, default_channels, delete_notify_group,
    list_notify_groups, save_notify_group,
)
from app.ai.prompts import (
    ACK_TEMPLATE_MAX, DEFAULT_ACK_ENABLED, DEFAULT_ACK_TEMPLATE,
    DEFAULT_PERSONA_NAME, DEFAULT_PERSONA_PRESET, DEFAULT_PERSONA_VERBOSITY,
    PERSONA_PRESETS, load_persona,
)
from app.services.settings_service import get_setting, get_setting_from, get_own_setting, set_setting

bp = Blueprint("settings_page", __name__, url_prefix="/settings")


def _is_admin() -> bool:
    return bool(getattr(current_user, "is_admin", False))


def _admin_denied():
    flash("仅管理员可执行该操作", "error")
    return redirect(url_for("settings_page.index"))


def _secret_view(key: str, env_key: str | None = None) -> dict:
    """敏感配置回显视图（防跨用户密钥泄露）。

    只回显当前用户自己配置的明文；自己未配置时回显空串，
    全局（user_id=0）/.env 的值不再下发到页面。
    using_global 供模板提示「正在使用系统全局配置（值已隐藏）」。
    """
    own = str(get_own_setting(key, "") or "").strip()
    using_global = False
    if not own:
        env_default = current_app.config.get(env_key, "") if env_key else ""
        using_global = bool(str(get_setting(key, env_default, user_id=0) or "").strip())
    return {"value": own, "using_global": using_global}

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


def _feishu_callback_url() -> str:
    """飞书事件回调地址：优先用配置的域名（后台域名 > 网页/主域名），避免 localhost/内网访问时生成错误地址。"""
    domain = (str(get_setting_from("admin_domain", "ADMIN_DOMAIN", "", user_id=0) or "").strip()
              or str(get_setting_from("page_domain", "PAGE_DOMAIN", "", user_id=0) or "").strip())
    if domain:
        return f"https://{domain}/feishu/event"
    return request.host_url.rstrip("/") + url_for("feishu.event")


def _api_token_view() -> dict:
    """当前用户 App Token：只回传是否已配置与结尾 4 位，不把明文送到页面。"""
    tok = (getattr(current_user, "api_token", None) or "").strip()
    return {
        "configured": bool(tok),
        "tail": tok[-4:] if len(tok) >= 4 else "",
    }


def _ai_view() -> dict:
    """AI 配置视图：只读当前用户自己的协议/地址/模型/Key，不借用别人或 .env 的 Key。"""
    from app.ai.llm import default_for_protocol, grouped_provider_presets, normalize_protocol
    from app.services.settings_service import get_own_setting

    protocol = normalize_protocol(get_own_setting("llm_protocol", "openai"))
    defaults = default_for_protocol(protocol)
    base_url = str(get_own_setting("llm_base_url", "") or "").strip() or defaults["base_url"]
    model = str(get_own_setting("llm_model", "") or "").strip() or defaults["model"]
    key = str(get_own_setting("llm_api_key", "") or "").strip()
    return {
        "protocol": protocol,
        "base_url": base_url,
        "model": model,
        "api_key_configured": bool(key),
        "api_key_tail": key[-4:] if len(key) >= 4 else (key or "****"),
        "preset_groups": grouped_provider_presets(),
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
        "sc_key": _secret_view("sc_key", "SC_KEY"),
        "feishu_webhook_url": _secret_view("feishu_webhook_url", "FEISHU_WEBHOOK_URL"),
        "feishu_secret": _secret_view("feishu_secret", "FEISHU_SECRET"),
        "default_channels": default_channels(),
    }
    scene_cfg = {}
    for scene, key in SCENE_KEYS.items():
        val = get_setting_from(key, None, None) or []
        scene_cfg[scene] = val[0] if isinstance(val, list) and val else (val or "")
    notify_groups = list_notify_groups()
    group_options = [
        (f"{GROUP_PREFIX}{name}", f"组：{name}")
        for name in notify_groups
    ]
    # 默认/场景渠道的可选项 = 真实渠道 + 自定义通知组（统一 list，模板直接遍历）
    all_channel_options = list(_CHANNEL_OPTIONS) + group_options
    briefing = {
        "morning": get_setting_from("briefing_time_morning", None, "07:00"),
        "noon": get_setting_from("briefing_time_noon", None, "12:00"),
        "evening": get_setting_from("briefing_time_evening", None, "21:00"),
    }
    from app.services.feishu_ws import receive_mode as feishu_receive_mode, status as feishu_ws_status

    feishu_app = {
        "app_id": get_setting_from("feishu_app_id", "FEISHU_APP_ID", ""),
        "app_secret": _secret_view("feishu_app_secret", "FEISHU_APP_SECRET"),
        "event_token": _secret_view("feishu_event_token", "FEISHU_EVENT_TOKEN"),
        "encrypt_key": _secret_view("feishu_event_encrypt_key", "FEISHU_EVENT_ENCRYPT_KEY"),
        "target": get_setting_from("feishu_app_target", "", ""),
        "target_type": get_setting_from("feishu_app_target_type", "", "chat_id"),
        "callback_url": _feishu_callback_url(),
        "receive_mode": feishu_receive_mode(),
        "ws": feishu_ws_status(),
        "configured": bool(get_setting_from("feishu_app_id", "FEISHU_APP_ID", "")
                           and get_setting_from("feishu_app_secret", "FEISHU_APP_SECRET", "")),
    }
    try:
        from app.utils.netinfo import PAGE_UNREACHABLE_MSG, diagnose_page_reachability

        feishu_app["net"] = diagnose_page_reachability()
        feishu_app["page_warn"] = PAGE_UNREACHABLE_MSG
    except Exception:  # noqa: BLE001
        feishu_app["net"] = {}
        feishu_app["page_warn"] = ""
    from app.services.backup_service import list_backups
    from app.utils.timeutil import fmt_dt, user_tz

    tz = user_tz(current_user)
    files = list_backups()
    for f in files:
        f["mtime_text"] = fmt_dt(f["mtime"], tz)  # naive UTC → 用户时区字符串
    backup = {
        "enabled": bool(get_setting_from("backup_enabled", None, True)),
        "time": get_setting_from("backup_time", None, "03:00"),
        "keep": get_setting_from("backup_keep", None, 7),
        "channel": get_setting_from("backup_channel", None, "inapp"),
        "files": files,
    }
    from app.services.page_service import page_port_configured, page_site_base_url
    from app.services.page_site_server import status as page_site_status

    port_val = page_port_configured()
    site_pages = {
        "admin_domain": str(get_setting_from("admin_domain", "ADMIN_DOMAIN", "", user_id=0) or ""),
        "page_domain": str(get_setting_from("page_domain", "PAGE_DOMAIN", "", user_id=0) or ""),
        "admin_entry": str(get_setting_from("admin_entry", "ADMIN_ENTRY", "", user_id=0) or ""),
        "page_port": str(port_val or get_setting_from("page_port", "PAGE_PORT", "") or ""),
        "page_host": str(get_setting_from("page_host", "PAGE_HOST", "", user_id=0) or ""),
        "page_base_url": page_site_base_url(),
        "page_site": page_site_status(),
    }
    tts_key = str(get_own_setting("tts_api_key", "") or "").strip()
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
        "serper_key": _secret_view("serper_api_key", "SERPER_API_KEY"),
        "tavily_key": _secret_view("tavily_api_key", "TAVILY_API_KEY"),
    }
    img_key = str(get_own_setting("image_api_key", "") or "").strip()
    image = {
        "base_url": str(get_setting_from("image_base_url", "IMAGE_BASE_URL", "") or ""),
        "model": str(get_setting_from("image_model", "IMAGE_MODEL", "") or ""),
        "size": str(get_setting_from("image_size", "IMAGE_SIZE", "1024x1024") or ""),
        "api_key_configured": bool(img_key),
        "api_key_tail": img_key[-4:] if len(img_key) >= 4 else (img_key or "****"),
    }
    vision_key = str(get_own_setting("vision_api_key", "") or "").strip()
    vision = {
        "base_url": str(get_setting_from("vision_base_url", "VISION_BASE_URL", "") or ""),
        "model": str(get_setting_from("vision_model", "VISION_MODEL", "") or ""),
        "api_key_configured": bool(vision_key),
        "api_key_tail": vision_key[-4:] if len(vision_key) >= 4 else (vision_key or "****"),
    }
    # 用户管理（仅管理员）
    users_list = []
    if getattr(current_user, "is_admin", False):
        from app.models.user import User

        users_list = [
            {"id": u.id, "username": u.username, "is_admin": u.is_admin,
             "timezone": u.timezone, "created_at": fmt_dt(u.created_at, tz),
             "feishu_open_id": u.feishu_open_id or ""}
            for u in User.query.order_by(User.id.asc()).all()
        ]
    return render_template(
        "settings/index.html",
        form=form, ai=ai, channels=channels, briefing=briefing,
        feishu_app=feishu_app, backup=backup, site_pages=site_pages, image=image,
        voice=voice, search_cfg=search_cfg, scene_cfg=scene_cfg, vision=vision,
        channel_options=_CHANNEL_OPTIONS,
        notify_groups=notify_groups,
        all_channel_options=all_channel_options,
        users=users_list, is_admin=getattr(current_user, "is_admin", False),
        api_token_view=_api_token_view(),
        persona=load_persona(current_user),
        persona_presets=PERSONA_PRESETS,
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


@bp.route("/users", methods=["POST"])
@login_required
def users():
    """管理员手动创建用户账号（自动补一套内置任务）。"""
    if not getattr(current_user, "is_admin", False):
        flash("仅管理员可创建用户", "error")
        return redirect(url_for("settings_page.index"))

    from app.models.user import User
    from app.services.install_service import seed_builtin_jobs_for_user

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    timezone = (request.form.get("timezone") or "Asia/Shanghai").strip() or "Asia/Shanghai"
    if len(username) < 2:
        flash("用户名至少 2 个字符", "error")
        return redirect(url_for("settings_page.index"))
    if len(password) < 6:
        flash("密码至少 6 位", "error")
        return redirect(url_for("settings_page.index"))
    if User.query.filter_by(username=username).first():
        flash("用户名已存在，请换一个", "error")
        return redirect(url_for("settings_page.index"))

    user = User(username=username, timezone=timezone, is_admin=False)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    seed_builtin_jobs_for_user(user)
    flash(f"已创建用户「{username}」", "success")
    return redirect(url_for("settings_page.index"))


@bp.route("/users/bind", methods=["POST"])
@login_required
def users_bind():
    """管理员为某个用户绑定/解绑飞书 open_id（发送者识别用）。"""
    if not getattr(current_user, "is_admin", False):
        flash("仅管理员可绑定飞书身份", "error")
        return redirect(url_for("settings_page.index"))

    from app.models.user import User

    try:
        uid = int(request.form.get("user_id") or 0)
    except (TypeError, ValueError):
        uid = 0
    clear = bool(request.form.get("clear"))
    open_id = "" if clear else (request.form.get("feishu_open_id") or "").strip()
    user = db.session.get(User, uid) if uid else None
    if user is None:
        flash("用户不存在", "error")
        return redirect(url_for("settings_page.index"))

    if open_id:
        if not open_id.startswith("ou_"):
            flash("飞书 open_id 格式不正确，应以 ou_ 开头", "error")
            return redirect(url_for("settings_page.index"))
        other = User.query.filter(User.feishu_open_id == open_id,
                                  User.id != user.id).first()
        if other is not None:
            flash(f"该 open_id 已绑定给用户「{other.username}」，不能重复绑定", "error")
            return redirect(url_for("settings_page.index"))

    user.feishu_open_id = open_id or None
    db.session.commit()
    if open_id:
        flash(f"已为用户「{user.username}」绑定飞书身份", "success")
    else:
        flash(f"已解除用户「{user.username}」的飞书绑定", "success")
    return redirect(url_for("settings_page.index"))


@bp.route("/channels", methods=["POST"])
@login_required
def channels():
    """保存通知渠道配置（sc_key / feishu_webhook_url / feishu_secret / default_channels / 各场景渠道）。"""
    set_setting("sc_key", (request.form.get("sc_key") or "").strip())
    set_setting("feishu_webhook_url", (request.form.get("feishu_webhook_url") or "").strip())
    set_setting("feishu_secret", (request.form.get("feishu_secret") or "").strip())
    # 合法可选值 = 真实渠道 + 已存在的通知组（group:组名）
    valid_values = set(dict(_CHANNEL_OPTIONS)) | {
        f"{GROUP_PREFIX}{n}" for n in list_notify_groups()
    }
    defaults = [c for c in request.form.getlist("default_channels")
                if c in valid_values]
    set_setting("default_channels", defaults or ["inapp"])
    # 各应用场景渠道（空 = 跟随全局默认）
    for scene, key in SCENE_KEYS.items():
        v = (request.form.get(f"scene_{scene}") or "").strip()
        set_setting(key, [v] if v and v in valid_values else [])
    flash("已保存", "success")
    return redirect(url_for("settings_page.index"))


@bp.route("/notify-group/add", methods=["POST"])
@login_required
def add_group():
    """新增或覆盖一个自定义通知组（组内多渠道联动）。"""
    name = (request.form.get("new_group_name") or "").strip()
    chans = request.form.getlist("new_group_channels")
    try:
        save_notify_group(name, chans)
        flash(f"已保存通知组「{name}」", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("settings_page.index"))


@bp.route("/notify-group/delete", methods=["POST"])
@login_required
def delete_group():
    """删除一个自定义通知组（并清理默认渠道/场景渠道对它的引用）。"""
    name = (request.form.get("delete_group") or "").strip()
    if name:
        delete_notify_group(name)
        flash(f"已删除通知组「{name}」", "success")
    return redirect(url_for("settings_page.index"))


@bp.route("/ai", methods=["POST"])
@login_required
def ai():
    """保存当前用户的 AI 模型配置（协议 / BaseURL / Model / API Key），立即生效。"""
    from app.ai.llm import normalize_protocol

    protocol = normalize_protocol(request.form.get("llm_protocol"))
    base_url = (request.form.get("llm_base_url") or "").strip().rstrip("/")
    model = (request.form.get("llm_model") or "").strip()
    api_key = (request.form.get("llm_api_key") or "").strip()
    clear_key = bool(request.form.get("clear_api_key"))

    if not base_url.startswith(("http://", "https://")):
        flash("接口地址必须以 http:// 或 https:// 开头", "error")
        return redirect(url_for("settings_page.index"))
    if not model:
        flash("模型名不能为空", "error")
        return redirect(url_for("settings_page.index"))

    set_setting("llm_protocol", protocol)
    set_setting("llm_base_url", base_url)
    set_setting("llm_model", model)
    if clear_key:
        set_setting("llm_api_key", "")
    elif api_key:
        set_setting("llm_api_key", api_key)
    flash("AI 配置已保存到你的账号，立即生效", "success")
    return redirect(url_for("settings_page.index"))


@bp.route("/persona", methods=["POST"])
@login_required
def persona():
    """保存对话人设（名字 / 预设 / 称呼 / 回复详细度 / 立即回复 / 补充说明）。下一轮对话立即生效。"""
    from app.ai.prompts import PERSONA_ADDRESS_MAX, PERSONA_EXTRA_MAX, PERSONA_NAME_MAX
    from app.ai.prompts import VERBOSITY_HINTS

    if request.form.get("reset_persona"):
        set_setting("ai_persona_name", DEFAULT_PERSONA_NAME)
        set_setting("ai_persona_preset", DEFAULT_PERSONA_PRESET)
        set_setting("ai_persona_verbosity", DEFAULT_PERSONA_VERBOSITY)
        set_setting("ai_persona_address", "")
        set_setting("ai_persona_extra", "")
        set_setting("ai_persona_ack_template", DEFAULT_ACK_TEMPLATE)
        set_setting("ai_persona_ack_enabled", DEFAULT_ACK_ENABLED)
        flash("已恢复默认人设，下一轮对话生效", "success")
        return redirect(url_for("settings_page.index") + "?tab=ai")

    name = (request.form.get("ai_persona_name") or "").strip()[:PERSONA_NAME_MAX] or DEFAULT_PERSONA_NAME
    preset = (request.form.get("ai_persona_preset") or DEFAULT_PERSONA_PRESET).strip()
    if preset not in PERSONA_PRESETS:
        flash("未知的人设预设", "error")
        return redirect(url_for("settings_page.index") + "?tab=ai")
    verbosity = (request.form.get("ai_persona_verbosity") or DEFAULT_PERSONA_VERBOSITY).strip()
    if verbosity not in VERBOSITY_HINTS:
        verbosity = DEFAULT_PERSONA_VERBOSITY
    address = (request.form.get("ai_persona_address") or "").strip()[:PERSONA_ADDRESS_MAX]
    extra = (request.form.get("ai_persona_extra") or "").strip()[:PERSONA_EXTRA_MAX]
    if preset == "custom" and not extra:
        flash("选择「自定义」时请填写补充说明", "error")
        return redirect(url_for("settings_page.index") + "?tab=ai")

    set_setting("ai_persona_name", name)
    set_setting("ai_persona_preset", preset)
    set_setting("ai_persona_verbosity", verbosity)
    set_setting("ai_persona_address", address)
    set_setting("ai_persona_extra", extra)
    # 旧表单/测试不带立即回复字段时保持原值，避免误关
    if "ai_persona_ack" in request.form:
        ack_template = (request.form.get("ai_persona_ack") or "").strip()[:ACK_TEMPLATE_MAX]
        ack_enabled = request.form.get("ai_persona_ack_enabled") == "1"
        if ack_enabled and not ack_template:
            ack_template = DEFAULT_ACK_TEMPLATE
        set_setting("ai_persona_ack_template", ack_template)
        set_setting("ai_persona_ack_enabled", ack_enabled)
    flash("人设已保存，下一轮对话立即生效", "success")
    return redirect(url_for("settings_page.index") + "?tab=ai")


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
    mode = (request.form.get("feishu_receive_mode") or "callback").strip()
    if mode not in ("callback", "sdk"):
        mode = "callback"
    set_setting("feishu_receive_mode", mode)
    # 全局接收模式影响飞书长连接的启停，仅管理员可写
    if _is_admin():
        set_setting("feishu_receive_mode", mode, user_id=0)
    try:
        from app.services.feishu_ws import restart as restart_feishu_ws

        restart_feishu_ws(current_app)
    except Exception:  # noqa: BLE001
        current_app.logger.exception("保存后重启飞书长连接失败")
        flash("配置已保存，但长连接启停失败，请查看日志或重启应用", "error")
        return redirect(url_for("settings_page.index") + "?tab=feishu-app")
    flash("飞书应用机器人配置已保存", "success")
    return redirect(url_for("settings_page.index") + "?tab=feishu-app")


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

    # 同步内置任务：cron = '分 时 * * *'，开关同步（仅本人任务）
    from app.models.scheduled_job import ACTION_DATA_BACKUP, ScheduledJob

    job = ScheduledJob.query.filter_by(
        job_key=ACTION_DATA_BACKUP, user_id=current_user.id).first()
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
    """下载备份文件（仅管理员；校验文件名，仅限备份目录内 backup-*.json）。"""
    from pathlib import Path

    from flask import send_file

    if not _is_admin():
        return _admin_denied()
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
    """删除一个备份文件（仅管理员；POST + CSRF）。"""
    from app.services.backup_service import delete_backup

    if not _is_admin():
        return _admin_denied()
    try:
        delete_backup(name)
        flash(f"已删除备份：{name}", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("settings_page.index"))


@bp.route("/export", methods=["POST"])
@login_required
def export_data():
    """一键导出（仅管理员）：生成最新备份 JSON 并直接下载到本地。"""
    from pathlib import Path

    from flask import send_file

    from app.services.backup_service import backup_to_json

    if not _is_admin():
        return _admin_denied()
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
    """一键重置（仅管理员）：清空业务数据（保留账号与系统配置），重置前自动备份。"""
    from app.services.backup_service import backup_to_json
    from app.services.data_service import reset_all_data

    if not _is_admin():
        return _admin_denied()
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
    """从备份恢复数据（仅管理员）：可上传备份文件，或选择服务器已有备份。"""
    import os
    import tempfile
    from pathlib import Path

    from app.services.backup_service import backup_to_json, restore_from_json

    if not _is_admin():
        return _admin_denied()
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
    job = ScheduledJob.query.filter_by(job_key=action, user_id=current_user.id).first()
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
    """保存网页站点（端口优先）+ 可选域名/安全入口（仅管理员：影响全局部署配置与 .env）。"""
    import re

    if not _is_admin():
        return _admin_denied()
    admin = (request.form.get("admin_domain") or "").strip().lower()
    page = (request.form.get("page_domain") or "").strip().lower()
    entry = (request.form.get("admin_entry") or "").strip().strip("/")
    host = (request.form.get("page_host") or "").strip()
    host = host.replace("http://", "").replace("https://", "").split("/")[0].split(":")[0]
    raw_port = (request.form.get("page_port") or "").strip()
    port = 0
    if raw_port:
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            flash("网页端口须为 1–65535 的数字，或留空", "error")
            return redirect(url_for("settings_page.index") + "?tab=pages-domain")
        if not (1 <= port <= 65535):
            flash("网页端口须为 1–65535", "error")
            return redirect(url_for("settings_page.index") + "?tab=pages-domain")
        main_port = int(current_app.config.get("MAIN_PORT") or 5000)
        if port == main_port:
            flash(f"网页端口不能与后台端口相同（{port}）", "error")
            return redirect(url_for("settings_page.index") + "?tab=pages-domain")
    domain_re = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
    for name, value in (("后台域名", admin), ("网页域名", page)):
        if not value:
            continue
        value = re.sub(r"^https?://", "", value).split("/")[0].split(":")[0].strip().rstrip(".")
        if not re.match(domain_re, value):
            flash(f"{name}格式不正确（如 example.com，不要带协议/端口/路径）", "error")
            return redirect(url_for("settings_page.index") + "?tab=pages-domain")
        if name == "后台域名":
            admin = value
        else:
            page = value
    if entry:
        if not re.match(r"^[A-Za-z0-9_-]{1,32}$", entry):
            flash("安全入口只能包含字母/数字/下划线/连字符，且不超过 32 位", "error")
            return redirect(url_for("settings_page.index") + "?tab=pages-domain")
    if admin and page and admin == page:
        flash("后台域名与网页域名不能相同", "error")
        return redirect(url_for("settings_page.index") + "?tab=pages-domain")
    set_setting("admin_domain", admin, user_id=0)
    set_setting("page_domain", page, user_id=0)
    set_setting("admin_entry", entry, user_id=0)
    set_setting("page_port", port, user_id=0)
    set_setting("page_host", host, user_id=0)
    current_app.config["ADMIN_ENTRY"] = entry
    current_app.config["ADMIN_DOMAIN"] = admin
    current_app.config["PAGE_PORT"] = port
    current_app.config["PAGE_HOST"] = host
    try:
        from dotenv import set_key as _dotenv_set_key

        from app.services.install_service import ENV_PATH, _ensure_env_file

        _ensure_env_file()
        _dotenv_set_key(ENV_PATH, "ADMIN_DOMAIN", admin, quote_mode="always")
        _dotenv_set_key(ENV_PATH, "PAGE_DOMAIN", page, quote_mode="always")
        _dotenv_set_key(ENV_PATH, "ADMIN_ENTRY", entry, quote_mode="always")
        _dotenv_set_key(ENV_PATH, "PAGE_PORT", str(port or ""), quote_mode="never")
        _dotenv_set_key(ENV_PATH, "PAGE_HOST", host, quote_mode="never")
    except Exception:  # noqa: BLE001
        current_app.logger.warning("网页站点设置已写入数据库，但写回 .env 失败")
    try:
        from app.services.page_site_server import restart as restart_page_site

        restart_page_site(current_app)
    except Exception:  # noqa: BLE001
        current_app.logger.exception("网页站点端口启停失败")
        flash("设置已保存，但网页端口启动失败，请换一个端口或重启应用", "error")
        return redirect(url_for("settings_page.index") + "?tab=pages-domain")
    if port:
        flash(f"已保存：网页站点 http 端口 {port}（无需 HTTPS）", "success")
    else:
        flash("已保存：未设置网页端口，公开页仍走后台 /p/<slug>", "success")
    return redirect(url_for("settings_page.index") + "?tab=pages-domain")


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
