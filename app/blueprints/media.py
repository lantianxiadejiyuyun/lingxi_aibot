"""Browser and token API entry points share exactly the same account-scoped services."""
from functools import wraps
import json
import time

from flask import Blueprint, Response, current_app, g, render_template, request, send_file, stream_with_context, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.services import media_service as service, storage_service as storage
from app.services.media_config import MediaError, read_config, save_config, parse_id
from app.utils.integration_api import api_authenticated, ApiError, error_response, json_body, pagination, success, register_api_errors

bp = Blueprint("media", __name__, url_prefix="/media")
api_bp = Blueprint("integration_media", __name__, url_prefix="/api/v1")
register_api_errors(api_bp)


@bp.get("/")
@login_required
def index():
    return render_template("media/index.html", api_base=url_for("media.api_root"))


def _uid():
    return int(getattr(g, "api_user", current_user).id)


def _guard(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (MediaError, storage.StorageError, ApiError) as exc:
            db.session.rollback()
            return error_response(str(exc), exc.status, exc.code)
        except (TypeError, ValueError, OverflowError):
            db.session.rollback()
            return error_response("参数格式不正确", 400, "invalid_request")
    return wrapped


def route(path, methods=("GET",), endpoint=None):
    def decorate(fn):
        name = endpoint or fn.__name__
        bp.add_url_rule("/api/" + path, endpoint=name, view_func=login_required(_guard(fn)), methods=methods)
        if path:
            api_bp.add_url_rule("/" + path, endpoint=name, view_func=api_authenticated(_guard(fn)), methods=methods)
        return fn
    return decorate


@route("", endpoint="api_root")
def root():
    return success({"capabilities": ["storage", "downloads", "native_agents", "event_replay"]})


@route("storage-locations", methods=("GET", "POST"))
def locations():
    if request.method == "POST":
        location = storage.create_location(_uid(), json_body())
        return success(location.to_dict(include_paths=True), 201)
    return success(storage.list_locations(_uid(), include_paths=bool(current_user.is_admin)))


@route("storage-locations/<int:location_id>/probe", methods=("POST",))
def probe(location_id):
    location_id = parse_id(location_id)
    return success(storage.probe_location(storage.get_location(_uid(), location_id)))


@route("files")
def files():
    return success(storage.list_files(_uid(), parse_id(request.args.get("storage_id", 0)), request.args.get("path", "")))


@route("files/download")
def download_file():
    path = storage.get_file(_uid(), parse_id(request.args.get("storage_id", 0)), request.args.get("path", ""))
    return send_file(path, as_attachment=True, conditional=True)


@route("media/config", methods=("GET", "PUT"))
def config():
    return success(save_config(_uid(), json_body()) if request.method == "PUT" else read_config(_uid()))


@route("media/status")
def status():
    from app.services.media_config import worker_status
    return success(worker_status())


@route("anime/search", methods=("POST",))
def search():
    from app.services.media_sources import search_resources, SourceError
    data = json_body()
    criteria = service.validate_requirement(data.get("requirement", data))
    try:
        found = search_resources(criteria, sources=read_config(_uid(), secrets=True)["sources"])
    except SourceError as exc:
        raise MediaError(str(exc), "search_failed", 502) from None
    # Feed keys and signed download queries stay server-side.
    from urllib.parse import urlsplit, urlunsplit
    def safe_url(url):
        parts = urlsplit(url or "")
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")) if parts.scheme != "magnet" else "magnet:已解析"
    return success([{**{k: c.get(k) for k in ("title", "kind", "fingerprint")},
                     "source_url": safe_url(c.get("source_url"))} for c in found])


@route("downloads", methods=("GET", "POST"))
def downloads():
    if request.method == "POST":
        return success(service.task_dict(service.create_task(_uid(), json_body())), 201)
    limit, offset = pagination()
    return success(service.list_tasks(_uid(), limit, offset))


@route("downloads/<int:task_id>")
def detail(task_id):
    return success(service.task_dict(service.get_task(_uid(), task_id), detail=True))


@route("downloads/<int:task_id>/actions", methods=("POST",))
def actions(task_id):
    return success(service.task_dict(service.action(_uid(), task_id, json_body().get("action"))))


@route("downloads/<int:task_id>/events")
def task_events(task_id):
    return success(service.events(_uid(), task_id, parse_id(request.args.get("after", 0), minimum=0)))


@route("download-events")
def all_events():
    return success(service.events(_uid(), after=parse_id(request.args.get("after", 0), minimum=0)))


@route("downloads/<int:task_id>/stream")
def event_stream(task_id):
    uid = _uid()
    service.get_task(uid, task_id)
    after = parse_id(request.headers.get("Last-Event-ID") or request.args.get("after", 0), minimum=0)
    slots = current_app.extensions["integration_ws"]["slots"]
    if not slots.acquire():
        raise MediaError("进度连接数已达上限", "connection_limit", 429)
    if not slots.authenticate(uid):
        slots.release()
        raise MediaError("此账号进度连接数已达上限", "connection_limit", 429)
    released = False
    def release():
        nonlocal released
        if not released:
            slots.release(uid)
            released = True
    @stream_with_context
    def generate():
        nonlocal after
        deadline = time.monotonic() + 25
        try:
            while time.monotonic() < deadline:
                db.session.remove()
                if request.path.startswith("/api/v1/"):
                    from app.utils.api_auth import resolve_api_user
                    if not resolve_api_user():
                        break
                batch = service.events(uid, task_id, after)
                for event in batch:
                    after = event["seq"]
                    yield f"id: {after}\nevent: media\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if not batch:
                    yield ": heartbeat\n\n"
                    time.sleep(1)
        finally:
            db.session.remove()
            release()
    response = Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    response.call_on_close(release)
    return response


@route("agent-runs", methods=("GET", "POST"))
def agent_runs():
    from app.services.native_agent_service import list_runs, queue_agent
    if request.method == "POST":
        data = json_body()
        return success(queue_agent(_uid(), data.get("prompt", ""), conversation_id=data.get("conversation_id"),
                                   allowed_tools=data.get("allowed_tools"), request_key=data.get("request_key")), 201)
    return success(list_runs(_uid()))


@route("agent-runs/<int:run_id>")
def agent_run(run_id):
    from app.services.native_agent_service import get_run
    return success(get_run(_uid(), parse_id(run_id)))


@route("agent-runs/<int:run_id>/cancel", methods=("POST",))
def cancel_run(run_id):
    from app.services.native_agent_service import cancel_agent
    return success(cancel_agent(_uid(), parse_id(run_id)))
