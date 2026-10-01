"""应用配置：从环境变量 / .env 加载。"""
import logging
import os
import secrets
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# Docker 使用可写目录中的运行配置；本地部署仍默认使用项目根 .env。
_env_location = (os.getenv("AIBOT_ENV_FILE") or "").strip()
ENV_PATH = Path(_env_location).expanduser() if _env_location else BASE_DIR / ".env"
if not ENV_PATH.is_absolute():
    ENV_PATH = BASE_DIR / ENV_PATH

logger = logging.getLogger(__name__)

# 已知弱 SECRET_KEY（模板占位符等）：发现即替换为随机值，防止会话伪造
_WEAK_SECRET_KEYS = {
    "please-generate-a-random-50-char-string", "dev-secret-change-me",
    "change-me", "change-me-now", "your-secret-key",
}


def _ensure_env_file() -> Path:
    """缺少运行配置时自动生成，或首次导入项目根 .env。

    部署时无需手动创建 .env：没有也能启动，安装向导（/setup）接管数据库等配置。
    - AIBOT_ENV_FILE 指定其它路径时，优先导入项目根 .env，之后只使用新路径
    - 没有旧配置时复制 .env.example（不存在则写最小模板）
    - SECRET_KEY 替换为随机值（避免占位符当密钥）
    - .env 已存在但 SECRET_KEY 为占位符/缺失时同样替换为随机值
    - SESSION_COOKIE_SECURE 默认 0：HTTP/局域网阶段可正常登录，上线 HTTPS 后改 1
    - 目录不可写时静默失败（应用照常启动，仍走安装向导）
    """
    env_path = ENV_PATH
    legacy_path = BASE_DIR / ".env"
    if env_path != legacy_path and not env_path.exists() and legacy_path.is_file():
        try:
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.write_text(legacy_path.read_text(encoding="utf-8"), encoding="utf-8")
            env_path.chmod(0o600)
        except OSError:
            logger.warning("无法导入运行配置 %s，请检查目录权限", env_path)
            return env_path
    if env_path.exists():
        # 文件已存在：若 SECRET_KEY 是占位符/缺失，替换为随机值并写回
        try:
            text = env_path.read_text(encoding="utf-8")
            out_lines = []
            changed = False
            has_key_line = False
            for line in text.splitlines():
                if line.startswith("SECRET_KEY="):
                    has_key_line = True
                    val = line.split("=", 1)[1].strip()
                    if not val or val in _WEAK_SECRET_KEYS:
                        out_lines.append(f"SECRET_KEY={secrets.token_hex(32)}")
                        changed = True
                        continue
                out_lines.append(line)
            if changed:
                env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
            elif not has_key_line:
                env_path.write_text(text.rstrip("\n") + "\n"
                                    f"SECRET_KEY={secrets.token_hex(32)}\n",
                                    encoding="utf-8")
        except OSError:
            pass  # 目录不可写：不阻断启动
        return env_path
    example = BASE_DIR / ".env.example"
    try:
        if example.exists():
            content = example.read_text(encoding="utf-8")
        else:
            content = (
                "# 灵犀 AiBot 自动生成的最小配置\n"
                "# 完整模板见 .env.example；各项配置也可在网页「设置」里修改（页面值优先）\n"
                "SECRET_KEY=please-generate-a-random-50-char-string\n"
                "SESSION_COOKIE_SECURE=0\n"
                "MYSQL_HOST=127.0.0.1\nMYSQL_PORT=3306\nMYSQL_USER=ai_bot\n"
                "MYSQL_PASSWORD=\nMYSQL_DB=ai_bot\n"
            )
        out_lines = []
        for line in content.splitlines():
            if line.startswith("SECRET_KEY="):
                out_lines.append(f"SECRET_KEY={secrets.token_hex(32)}")
            elif line.startswith("SESSION_COOKIE_SECURE="):
                out_lines.append("# 自动生成默认 0：HTTP/局域网阶段可正常登录；上线 HTTPS 后请改为 1")
                out_lines.append("SESSION_COOKIE_SECURE=0")
            else:
                out_lines.append(line)
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        env_path.chmod(0o600)
    except OSError:
        pass  # 目录不可写：不阻断启动
    return env_path


load_dotenv(_ensure_env_file())


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    """环境变量布尔解析：兼容 1/true/yes/on（防止 'true' 被静默当成关闭）。"""
    raw = (os.getenv(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off", ""):
        return False
    return default


def _resolve_secret_key() -> str:
    """SECRET_KEY：占位符/缺失时生成随机值（Docker env 注入等无法写回 .env 的场景兜底）。

    注意：随机兜底值仅进程生命周期内有效，重启后会话会失效；
    日志会提示用户修正 .env 的 SECRET_KEY。
    """
    key = (os.getenv("SECRET_KEY") or "").strip()
    if key and key not in _WEAK_SECRET_KEYS:
        return key
    logger.warning(
        "SECRET_KEY 缺失或仍为占位符，已生成随机临时密钥（重启后会话将失效）。"
        "请尽快在 .env 中设置随机长字符串的 SECRET_KEY 后重启。")
    return secrets.token_hex(32)


class Config:
    # ---- 基础 ----
    SECRET_KEY = _resolve_secret_key()
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 7 * 24 * 3600
    APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Shanghai")
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    # ---- App 对接 API 鉴权 ----
    # 留空 = 禁用 /api/v1/* 接口（返回 401）；设置后 App 需在请求头携带该 Token。
    # 过短的 Token 视为未配置（最小 16 位）。
    _API_TOKEN_RAW = os.getenv("API_TOKEN", "").strip()
    API_TOKEN_MIN_LEN = 16
    if _API_TOKEN_RAW and len(_API_TOKEN_RAW) < API_TOKEN_MIN_LEN:
        logger.warning("API_TOKEN 长度不足 %s 位，已忽略（视为未配置）", API_TOKEN_MIN_LEN)
        API_TOKEN = ""
    else:
        API_TOKEN = _API_TOKEN_RAW

    # ---- MySQL ----
    MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT = _int("MYSQL_PORT", 3306)
    MYSQL_USER = os.getenv("MYSQL_USER", "ai_bot")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DB = os.getenv("MYSQL_DB", "ai_bot")
    SQLALCHEMY_DATABASE_URI = (
        # quote 而非 quote_plus：空格编码为 %20，SQLAlchemy unquote 才能正确还原
        # （quote_plus 会把空格转 +，且 SQLAlchemy 不会把 + 解码回空格）
        f"mysql+pymysql://{quote(MYSQL_USER, safe='')}:{quote(MYSQL_PASSWORD, safe='')}"
        f"@{MYSQL_HOST}:{MYSQL_PORT}/{quote(MYSQL_DB, safe='')}?charset=utf8mb4"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 28000,
        "pool_size": 5,
        "max_overflow": 10,
        # 数据库不可用时快速失败（首次安装向导需要快速探测，避免页面长时间卡住）
        "connect_args": {"connect_timeout": 5},
    }

    # ---- AI（OpenAI 兼容协议，默认 DeepSeek）----
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    LLM_API_KEY = os.getenv("LLM_API_KEY", "")
    LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
    # openai = OpenAI 兼容 chat.completions；anthropic = Claude Messages API
    LLM_PROTOCOL = os.getenv("LLM_PROTOCOL", "openai")
    LLM_TIMEOUT = _int("LLM_TIMEOUT", 90)
    LLM_MAX_TOOL_ROUNDS = 8  # 单轮对话工具调用最大轮数，防死循环

    # ---- 通知渠道默认值（可被设置页覆盖）----
    SC_KEY = os.getenv("SC_KEY", "")
    FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")
    FEISHU_SECRET = os.getenv("FEISHU_SECRET", "")
    FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
    FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
    FEISHU_EVENT_TOKEN = os.getenv("FEISHU_EVENT_TOKEN", "")
    FEISHU_EVENT_ENCRYPT_KEY = os.getenv("FEISHU_EVENT_ENCRYPT_KEY", "")
    # callback = HTTP Webhook；sdk = 官方 lark-oapi 长连接
    FEISHU_RECEIVE_MODE = os.getenv("FEISHU_RECEIVE_MODE", "callback")
    DEFAULT_CHANNELS = os.getenv("DEFAULT_CHANNELS", "inapp")

    # ---- 管理员（仅首次初始化使用）----
    # ADMIN_PASSWORD 留空时，CLI 初始化会自动生成随机密码并打印一次（不再使用弱默认口令）
    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

    # ---- 调度 ----
    SCHEDULER_ENABLED = _bool("SCHEDULER_ENABLED", True)
    SCHEDULER_TIMEZONE = APP_TIMEZONE
    DATA_DIR = BASE_DIR / "data"
    BACKUP_DIR = DATA_DIR / "backups"

    # ---- 网页站点（路径固定 /webs/html/<slug>，不再按域名分流）----
    # 独立网页 HTTP 端口（可选）：如 8080 → http://IP:8080/webs/html/<slug>；0/空 = 走后台端口同一路径
    PAGE_PORT = _int("PAGE_PORT", 0)
    PAGE_HOST = os.getenv("PAGE_HOST", "").strip()  # 链接里显示的主机，留空则自动用局域网 IPv4
    PAGE_PUBLIC_BASE_URL = os.getenv("PAGE_PUBLIC_BASE_URL", "")  # 完整公网根地址，仅用于公开分享链接
    # 网页站点监听地址：默认 127.0.0.1；局域网访问独立端口可改为 0.0.0.0
    PAGE_BIND = os.getenv("PAGE_BIND", "127.0.0.1").strip() or "127.0.0.1"
    MAIN_PORT = _int("PORT", 5000)
    # 允许把 LLM/图片/TTS 等 provider 指到本机/局域网（Ollama 等）；默认关，防 SSRF
    ALLOW_LOCAL_PROVIDERS = _bool("ALLOW_LOCAL_PROVIDERS", False)

    # 兼容旧 .env：不再用于访问分流
    ADMIN_DOMAIN = os.getenv("ADMIN_DOMAIN", "")
    PAGE_DOMAIN = os.getenv("PAGE_DOMAIN", "")

    # ---- 后台短入口（推荐）----
    # 设置后后台须访问 /<入口>/login；不带入口访问后台 404。
    # 公开网页仍是 /webs/html/<slug>，局域网 IP 可直达。
    # 如 ADMIN_ENTRY=abc123 → http://192.168.1.8:8000/abc123/login
    ADMIN_ENTRY = os.getenv("ADMIN_ENTRY", "").strip().strip("/")

    # ---- 图片生成（OpenAI 兼容 images 接口，可在设置页修改）----
    IMAGE_BASE_URL = os.getenv("IMAGE_BASE_URL", "")
    IMAGE_API_KEY = os.getenv("IMAGE_API_KEY", "")
    IMAGE_MODEL = os.getenv("IMAGE_MODEL", "")
    IMAGE_SIZE = os.getenv("IMAGE_SIZE", "1024x1024")
    IMAGE_DIR = DATA_DIR / "images"

    # ---- 视觉（多模态识图）模型：OpenAI 兼容 chat/completions + 图片输入，可在设置页修改；
    #      未配置时收到图片仅保存不识别（与沟通模型、图片生成模型相互独立）----
    VISION_BASE_URL = os.getenv("VISION_BASE_URL", "")
    VISION_API_KEY = os.getenv("VISION_API_KEY", "")
    VISION_MODEL = os.getenv("VISION_MODEL", "")

    # ---- 语音（OpenAI 兼容 TTS 接口，可在设置页修改；未配置时前端用浏览器朗读）----
    TTS_BASE_URL = os.getenv("TTS_BASE_URL", "")
    TTS_API_KEY = os.getenv("TTS_API_KEY", "")
    TTS_MODEL = os.getenv("TTS_MODEL", "")
    TTS_VOICE = os.getenv("TTS_VOICE", "alloy")

    # ---- RAG 语义检索（OpenAI 兼容 embeddings 接口，未配置时自动降级关键词搜索）----
    EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "")
    EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "")

    # ---- 联网搜索（可插拔提供方，可在设置页修改）----
    SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "bing")   # bing/duckduckgo/searxng/serper/tavily
    SEARXNG_BASE_URL = os.getenv("SEARXNG_BASE_URL", "")
    SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")
    TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

    # ---- 限流 ----
    RATELIMIT_STORAGE_URI = "memory://"
    RATELIMIT_STRATEGY = "fixed-window"
