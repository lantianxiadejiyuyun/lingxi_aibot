"""应用配置：从环境变量 / .env 加载。"""
import os
import secrets
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def _ensure_env_file() -> Path:
    """项目根缺少 .env 时自动生成（复制 .env.example 并填入随机 SECRET_KEY）。

    部署时无需手动创建 .env：没有也能启动，安装向导（/setup）接管数据库等配置。
    - 复制 .env.example（不存在则写最小模板）
    - SECRET_KEY 替换为随机值（避免占位符当密钥）
    - SESSION_COOKIE_SECURE 默认 0：HTTP/局域网阶段可正常登录，上线 HTTPS 后改 1
    - 目录不可写时静默失败（应用照常启动，仍走安装向导）
    """
    env_path = BASE_DIR / ".env"
    if env_path.exists():
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
        env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    except OSError:
        pass  # 目录不可写：不阻断启动
    return env_path


load_dotenv(_ensure_env_file())


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class Config:
    # ---- 基础 ----
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    SESSION_COOKIE_SECURE = _int("SESSION_COOKIE_SECURE", 0) == 1
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 7 * 24 * 3600
    APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Shanghai")
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    # ---- MySQL ----
    MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT = _int("MYSQL_PORT", 3306)
    MYSQL_USER = os.getenv("MYSQL_USER", "ai_bot")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DB = os.getenv("MYSQL_DB", "ai_bot")
    SQLALCHEMY_DATABASE_URI = (
        f"mysql+pymysql://{quote_plus(MYSQL_USER)}:{quote_plus(MYSQL_PASSWORD)}"
        f"@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}?charset=utf8mb4"
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
    LLM_TIMEOUT = _int("LLM_TIMEOUT", 90)
    LLM_MAX_TOOL_ROUNDS = 8  # 单轮对话工具调用最大轮数，防死循环

    # ---- 通知渠道默认值（可被设置页覆盖）----
    SC_KEY = os.getenv("SC_KEY", "")
    FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")
    FEISHU_SECRET = os.getenv("FEISHU_SECRET", "")
    DEFAULT_CHANNELS = os.getenv("DEFAULT_CHANNELS", "inapp")

    # ---- 管理员（仅首次初始化使用）----
    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

    # ---- 调度 ----
    SCHEDULER_ENABLED = _int("SCHEDULER_ENABLED", 1) == 1
    SCHEDULER_TIMEZONE = APP_TIMEZONE
    DATA_DIR = BASE_DIR / "data"
    BACKUP_DIR = DATA_DIR / "backups"

    # ---- 域名体系（后台与网页域名分离，可在设置页修改）----
    ADMIN_DOMAIN = os.getenv("ADMIN_DOMAIN", "")   # 后台域名，如 admin.eugenstudio.cn（留空不限制）
    PAGE_DOMAIN = os.getenv("PAGE_DOMAIN", "")     # AI 网页域名，如 web.eugenstudio.cn（留空则用当前主机路径 /p/<slug>）

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
