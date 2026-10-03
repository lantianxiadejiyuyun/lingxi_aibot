"""Temporary loopback server for Node -> real Flask integration tests.

Run with the project virtualenv, pass a new directory outside the checkout.
Only synthetic accounts are created. No model or production HTTP is allowed.
Terminate this process after the coordinating client's tests are complete.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.tests.support import make_app
from app.extensions import db
from app.models.user import User
from werkzeug.serving import make_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    # Refuse to reuse a database or overwrite a prior fixture's credentials.
    directory.mkdir(parents=True, exist_ok=False)
    database = directory / "fixture.sqlite"
    app = make_app(
        ADMIN_ENTRY="lingxi", API_TOKEN="",
        SQLALCHEMY_DATABASE_URI="sqlite:///" + database.as_posix(),
        DATA_DIR=directory, IMAGE_DIR=directory / "images", BACKUP_DIR=directory / "backups",
    )
    accounts = []
    with app.app_context():
        db.create_all()
        for name in ("synthetic_owner", "synthetic_other"):
            token = "lx_fixture_" + secrets.token_urlsafe(24)
            password = "fixture_" + secrets.token_urlsafe(20)
            user = User(username=name, api_token=token, timezone="Asia/Shanghai", is_admin=False)
            user.set_password(password)
            db.session.add(user)
            db.session.flush()
            accounts.append({"id": user.id, "username": name, "password": password, "api_token": token})
        db.session.commit()
    server = make_server("127.0.0.1", args.port, app, threaded=True)
    base = f"http://127.0.0.1:{server.server_port}/lingxi"
    ready = {"synthetic_only": True, "base_url": base, "api_base": base + "/api/v1",
             "ws_url": base.replace("http://", "ws://") + "/api/v1/ws", "users": accounts,
             "chat_message": "/help", "notes": "Real Flask routes; all upstream HTTP disabled; temporary SQLite only."}
    ready_path = directory / "ready.json"
    ready_path.write_text(json.dumps(ready, indent=2), encoding="utf-8")
    print(f"Synthetic integration server ready: {ready_path}", flush=True)
    try:
        with patch("requests.sessions.Session.request", side_effect=RuntimeError("Fixture blocks upstream HTTP")), \
                patch("httpx.Client.send", side_effect=RuntimeError("Fixture blocks upstream HTTP")):
            server.serve_forever()
    finally:
        server.server_close()
        with app.app_context():
            db.session.remove()
            db.engine.dispose()


if __name__ == "__main__":
    main()
