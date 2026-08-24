"""Flask 扩展单例，避免循环导入。"""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect

db = SQLAlchemy()


def recover_session() -> None:
    """回滚失败事务，避免 PendingRollbackError 污染后续查询。"""
    try:
        db.session.rollback()
    except Exception:  # noqa: BLE001
        try:
            db.session.remove()
        except Exception:  # noqa: BLE001
            pass
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["300 per minute"],
    storage_uri="memory://",
)
