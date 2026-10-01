"""Isolated Flask/SQLite fixture: no project .env, live database or HTTP calls."""
from __future__ import annotations

import atexit
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

# Config is loaded once per process. Keep its automatically generated env outside
# the checkout, including when the test runner is invoked in a configured project.
_config_directory = tempfile.TemporaryDirectory(prefix="lingxi-test-config-")
atexit.register(_config_directory.cleanup)
with patch.dict(os.environ, {
    "AIBOT_ENV_FILE": str(Path(_config_directory.name) / ".env"),
    "SECRET_KEY": "isolated-regression-test-secret",
}):
    from app.config import Config
    from app import create_app

from app.extensions import db
from app.models import User
from app.utils.scoping import clear_current_user_id


def make_app(**overrides):
    class TestConfig(Config):
        TESTING = True
        SECRET_KEY = "isolated-regression-test-secret"
        SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        SQLALCHEMY_ENGINE_OPTIONS = {}
        SCHEDULER_ENABLED = False
        RATELIMIT_ENABLED = False
        SESSION_COOKIE_SECURE = False
        ADMIN_ENTRY = ""
        PAGE_PORT = 0
        APP_TIMEZONE = "Asia/Shanghai"
        SCHEDULER_TIMEZONE = "Asia/Shanghai"
        FEISHU_RECEIVE_MODE = "callback"
        FEISHU_APP_ID = ""
        FEISHU_APP_SECRET = ""
        LLM_API_KEY = ""
        IMAGE_API_KEY = ""
        VISION_API_KEY = ""
        TTS_API_KEY = ""
        EMBEDDING_API_KEY = ""
        SC_KEY = ""
        FEISHU_WEBHOOK_URL = ""
        DEFAULT_CHANNELS = "inapp"
        LOG_LEVEL = "ERROR"
        DATA_DIR = Path(_config_directory.name) / "data"
        IMAGE_DIR = DATA_DIR / "images"
        BACKUP_DIR = DATA_DIR / "backups"

    for key, value in overrides.items():
        setattr(TestConfig, key, value)
    with patch("app.services.skill_service.load_skills", return_value=[]), \
            patch("app.services.feishu_ws.start_if_needed"), \
            patch("app.services.page_site_server.start_if_needed"):
        app = create_app(TestConfig)

    def clear_request_caches():
        # Service tests keep an app context for ORM access. Production instead has
        # a fresh g per request, so do not let Flask-Login/CSRF caches leak between
        # clients inside that deliberately long-lived test context.
        from flask import g
        for key in ("_login_user", "csrf_token", "csrf_valid"):
            g.pop(key, None)

    app.before_request_funcs.setdefault(None, []).insert(0, clear_request_caches)
    return app


class IsolatedAppTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory(prefix="lingxi-test-data-")
        self.addCleanup(self.temp_directory.cleanup)
        data_dir = Path(self.temp_directory.name)
        self.app = make_app(DATA_DIR=data_dir, IMAGE_DIR=data_dir / "images",
                            BACKUP_DIR=data_dir / "backups")
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.addCleanup(clear_current_user_id)
        self.addCleanup(db.session.remove)
        db.create_all()
        for target in ("requests.sessions.Session.request", "httpx.Client.send"):
            blocker = patch(target, side_effect=AssertionError("External HTTP disabled in tests"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def make_user(self, username="review_admin", is_admin=True):
        user = User(username=username, is_admin=is_admin, timezone="Asia/Shanghai")
        user.set_password("isolated-test-password")
        db.session.add(user)
        db.session.commit()
        return user

    def client_for(self, user):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(user.id)
            session["_fresh"] = True
        return client
