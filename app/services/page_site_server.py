"""独立 HTTP 网页站点：只提供公开网页和公开图片，不配 HTTPS / 域名。

设置页填写「网页端口」后，在本进程再开一个监听（默认 0.0.0.0），
访问 http://<主机>:<端口>/<slug>。改端口保存后热启停。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from flask import Flask, Response, abort, send_file

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_httpd = None
_thread: Optional[threading.Thread] = None
_status: dict[str, Any] = {"running": False, "port": 0, "error": ""}


def status() -> dict[str, Any]:
    alive = bool(_thread and _thread.is_alive())
    return {
        "running": bool(_status["running"] and alive),
        "port": int(_status.get("port") or 0),
        "error": _status.get("error") or "",
        "alive": alive,
    }


def create_page_site_app(main_app: Flask) -> Flask:
    """仅公开网页 + 公开图片的迷你应用（与后台 Flask 实例分离）。"""
    site = Flask("aibot-pages")
    site.config["IMAGE_DIR"] = main_app.config.get("IMAGE_DIR")

    @site.get("/")
    def index():
        return Response(
            "<!doctype html><meta charset=utf-8><title>灵犀网页</title>"
            "<p style='font-family:sans-serif;padding:2rem'>这是网页站点端口。"
            "请访问 <code>/页面slug</code>。</p>",
            mimetype="text/html; charset=utf-8",
        )

    @site.get("/p/<slug>")
    @site.get("/<slug>")
    def serve_page(slug: str):
        if "/" in slug or not slug:
            abort(404)
        with main_app.app_context():
            from app.services import page_service

            page = page_service.visible_by_slug(slug)
            if page is None or not page.is_public:
                abort(404)
            return Response(page.content or "", mimetype="text/html; charset=utf-8")

    @site.get("/img/<path:filename>")
    def serve_image(filename: str):
        import re

        filename = filename.replace("\\", "/").split("/")[-1]
        if not re.match(r"^img-[a-f0-9]{8}-\d{1,6}\.(png|jpg|jpeg|webp|gif)$", filename):
            abort(404)
        with main_app.app_context():
            from pathlib import Path

            from app.services import image_service

            asset = image_service.get_by_filename(filename)
            if asset is None or not asset.is_public:
                abort(404)
            path = Path(main_app.config["IMAGE_DIR"]) / filename
            if not path.exists():
                abort(404)
            resp = send_file(path, conditional=True)
            resp.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
            return resp

    return site


def stop() -> None:
    global _httpd, _thread
    with _lock:
        httpd = _httpd
        _httpd = None
        _status["running"] = False
        _status["port"] = 0
    if httpd is not None:
        try:
            httpd.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("网页站点端口关闭失败")
    _thread = None


def start_if_needed(main_app: Flask) -> None:
    """按 settings/env 的 page_port 启停独立 HTTP 站点。"""
    from app.services.page_service import page_port_configured

    port = page_port_configured()
    if not port:
        stop()
        _status["error"] = ""
        return

    main_port = int(main_app.config.get("MAIN_PORT") or 0)
    if not main_port:
        import os

        try:
            main_port = int(os.getenv("PORT", "5000"))
        except (TypeError, ValueError):
            main_port = 5000
    if port == main_port:
        stop()
        _status["error"] = f"网页端口不能与后台端口相同（{port}）"
        logger.warning(_status["error"])
        return

    stop()
    site = create_page_site_app(main_app)
    try:
        from werkzeug.serving import make_server

        httpd = make_server("0.0.0.0", port, site, threaded=True)
    except OSError as e:
        _status["error"] = f"端口 {port} 无法监听：{e}"
        logger.exception("网页站点监听失败 port=%s", port)
        return

    def _run():
        global _status
        try:
            logger.info("网页站点 HTTP 已监听 0.0.0.0:%s", port)
            httpd.serve_forever()
        except Exception:  # noqa: BLE001
            logger.exception("网页站点线程退出")
        finally:
            _status["running"] = False

    t = threading.Thread(target=_run, name="page-site", daemon=True)
    with _lock:
        global _httpd, _thread
        _httpd = httpd
        _thread = t
        _status["running"] = True
        _status["port"] = port
        _status["error"] = ""
    t.start()


def restart(main_app: Flask) -> None:
    start_if_needed(main_app)
