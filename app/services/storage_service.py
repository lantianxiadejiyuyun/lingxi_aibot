"""Local and host-mounted NAS storage with a strict, user-owned root boundary.

SMB/NFS credentials and mounting remain host responsibilities. The app and the
downloader see the same share through separately configured directory mappings.
A marker lives on the actual filesystem: an empty directory left after a mount
disappears must never receive downloads. Workers do not recreate missing roots.
"""
from __future__ import annotations

import errno
import hashlib
import hmac
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import uuid

from app.extensions import db
from app.models.storage import StorageLocation
from app.models.user import User


_PENDING = ".lingxi-pending"
_MARKER_PREFIX = ".lingxi-storage-"
_COPY_PREFIX = ".lingxi-copy-"
_DEFAULT_RESERVE = 256 * 1024 * 1024
_MAX_TREE_ENTRIES = 100000


class StorageError(ValueError):
    def __init__(self, message: str, code: str = "storage_error", status: int = 400):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


def _int(value, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 2**63 - 1:
        raise StorageError(f"{name} 必须是大于等于 {minimum} 的整数", "invalid_argument")
    return value


def _text(value, name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise StorageError(f"{name} 不能为空且不能超过 {max_length} 个字符", "invalid_argument")
    if any(ord(char) < 32 for char in value):
        raise StorageError(f"{name} 含有无效字符", "invalid_argument")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise StorageError(f"{name} 含有无效字符", "invalid_argument") from exc
    return value.strip()


def _is_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    # Windows junctions are reparse points even where Path.is_symlink is false.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _reject_links(path: Path) -> None:
    for candidate in (path, *path.parents):
        if _is_link(candidate):
            raise StorageError("存储路径不能经过符号链接或目录联接", "unsafe_path")


def _mount_backed(root: Path) -> bool:
    """Require an actual mount during NAS registration, including Docker binds."""
    if os.name == "nt":
        if str(root).startswith("\\\\"):
            return True
        import ctypes
        return ctypes.windll.kernel32.GetDriveTypeW(str(root.anchor)) == 4  # DRIVE_REMOTE
    # Bind mounts may share st_dev with their parent, so ismount alone cannot
    # identify every Docker-mapped directory. mountinfo covers that case.
    try:
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) < 6:
                continue
            raw = fields[4]
            for escaped, plain in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
                raw = raw.replace(escaped, plain)
            mounted = Path(raw)
            if mounted != Path("/") and (mounted == root or mounted in root.parents):
                return True
    except OSError:
        pass
    return any(path != Path(path.anchor) and os.path.ismount(path) for path in (root, *root.parents))


def _parts(relative_path: str) -> tuple[str, ...]:
    if not isinstance(relative_path, str) or len(relative_path) > 2048:
        raise StorageError("文件路径无效", "unsafe_path")
    if not relative_path:
        return ()
    value = relative_path.replace("\\", "/")
    # Reject both operating systems' absolute syntax, ADS, dot segments and
    # Windows normalisation aliases, irrespective of the server OS.
    if value.startswith("/") or any(char in value for char in ':<>"|?*') or any(ord(char) < 32 for char in value):
        raise StorageError("只能访问存储位置内的相对路径", "unsafe_path")
    parts = value.split("/")
    if any(not part or part in (".", "..") or part.endswith((".", " ")) for part in parts):
        raise StorageError("文件路径包含不安全的目录段", "unsafe_path")
    devices = {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(10)), *("LPT" + str(i) for i in range(10))}
    if any(part.split(".", 1)[0].upper() in devices for part in parts):
        raise StorageError("文件路径不能使用系统设备名称", "unsafe_path")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise StorageError("文件路径无效", "unsafe_path") from exc
    return tuple(parts)


def _private(parts: tuple[str, ...]) -> bool:
    # Windows filesystems resolve names case-insensitively; the privacy gate
    # must not permit an uppercase spelling of a pending directory or marker.
    return any(part.casefold() == _PENDING or part.casefold().startswith((_MARKER_PREFIX, _COPY_PREFIX)) for part in parts)


def _root(location: StorageLocation) -> Path:
    if not location.enabled:
        raise StorageError("此存储位置已停用", "storage_disabled", 409)
    root = Path(location.root_path)
    try:
        if not root.is_absolute() or not root.is_dir():
            raise StorageError("存储目录不可用，请检查硬盘或 NAS 挂载", "storage_offline", 503)
        _reject_links(root)
        marker = root / location.marker_name
        if _is_link(marker) or not marker.is_file():
            raise StorageError("存储身份标记丢失，请恢复原硬盘或 NAS 挂载", "storage_offline", 503)
        if marker.stat().st_size > 128:
            raise StorageError("存储身份标记不匹配", "storage_identity_changed", 503)
        if not hmac.compare_digest(marker.read_bytes(), location.marker_token.encode("ascii")):
            raise StorageError("存储身份标记不匹配", "storage_identity_changed", 503)
        # Remote filesystem device numbers can change after a genuine remount.
        # The unique marker remains authoritative; local disks additionally
        # retain the original device guard.
        if location.kind == "mount" and not _mount_backed(root):
            raise StorageError("NAS 挂载不可用，请恢复原挂载", "storage_offline", 503)
        if location.kind == "local" and str(root.stat().st_dev) != location.root_device:
            raise StorageError("存储设备已变更，请重新登记存储位置", "storage_identity_changed", 503)
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError("无法访问存储位置，请检查挂载及权限", "storage_offline", 503) from exc
    return root


def create_location(user_id: int, data: dict) -> StorageLocation:
    """Register an existing directory; only admins can expose server paths."""
    user = db.session.get(User, user_id)
    if user is None or not user.is_admin:
        raise StorageError("仅管理员可以登记服务器存储路径", "admin_required", 403)
    if not isinstance(data, dict):
        raise StorageError("存储配置必须是对象", "invalid_argument")
    unknown = set(data) - {"name", "kind", "root_path", "download_path", "min_free_bytes", "owner_id"}
    if unknown:
        raise StorageError("存储配置含不支持的字段", "invalid_argument")
    owner_id = _int(data.get("owner_id", user_id), "所属用户编号", minimum=1)
    if db.session.get(User, owner_id) is None:
        raise StorageError("所属用户不存在，请检查账号编号", "owner_not_found", 404)
    name = _text(data.get("name"), "名称", 120)
    kind = data.get("kind", "local")
    if kind not in ("local", "mount"):
        raise StorageError("存储类型必须是 local 或 mount", "invalid_argument")
    root = Path(_text(data.get("root_path"), "存储根目录", 1024))
    if not root.is_absolute():
        raise StorageError("存储根目录必须是绝对路径", "invalid_argument")
    try:
        _reject_links(root)
        if not root.is_dir():
            raise StorageError("请先建立本机目录或挂载 NAS 后再登记", "storage_offline", 503)
        root = root.resolve(strict=True)
        root_device = str(root.stat().st_dev)
        if kind == "mount" and not _mount_backed(root):
            raise StorageError("此路径尚未挂载 NAS，请先在宿主机挂载并映射目录", "storage_offline", 503)
    except OSError as exc:
        raise StorageError("无法访问存储目录，请检查挂载及权限", "storage_offline", 503) from exc
    download_path = _text(data.get("download_path") or str(root), "下载器映射目录", 1024)
    if not (PurePosixPath(download_path).is_absolute() or PureWindowsPath(download_path).is_absolute()):
        raise StorageError("下载器映射目录必须是绝对路径", "invalid_argument")
    min_free_bytes = _int(data.get("min_free_bytes", _DEFAULT_RESERVE), "预留空间")
    # Prevent one user's registration from exposing another user's nested root.
    for other in StorageLocation.query.all():
        other_root = Path(other.root_path)
        if other_root == root or root in other_root.parents or other_root in root.parents:
            raise StorageError("此目录与已有存储位置重叠，请选择独立目录", "storage_overlap", 409)
    marker_token = uuid.uuid4().hex + uuid.uuid4().hex
    marker_name = _MARKER_PREFIX + uuid.uuid4().hex
    marker = root / marker_name
    try:
        with marker.open("x", encoding="ascii") as handle:
            handle.write(marker_token)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            marker.chmod(0o600)
    except OSError as exc:
        raise StorageError("存储目录不可写，请检查挂载及权限", "storage_readonly", 503) from exc
    location = StorageLocation(
        user_id=owner_id, name=name, kind=kind, root_path=str(root), download_path=download_path,
        marker_name=marker_name, marker_token=marker_token, root_device=root_device,
        min_free_bytes=min_free_bytes, enabled=True,
    )
    db.session.add(location)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        # Only remove the exact marker created by this failed registration.
        try:
            marker.unlink()
        except OSError:
            pass
        raise
    return location


def get_location(user_id: int, location_id: int) -> StorageLocation:
    location = StorageLocation.query.filter_by(id=location_id, user_id=user_id).first()
    if location is None:
        raise StorageError("存储位置不存在", "storage_not_found", 404)
    return location


def list_locations(user_id: int, *, include_paths: bool = False) -> list[dict]:
    locations = StorageLocation.query.filter_by(user_id=user_id).order_by(StorageLocation.id).all()
    return [location.to_dict(include_paths=include_paths) for location in locations]


def probe_location(location: StorageLocation, required_bytes: int = 0) -> dict:
    required_bytes = _int(required_bytes, "所需空间")
    result = {"id": location.id, "ok": False, "required_bytes": required_bytes}
    try:
        root = _root(location)
        usage = shutil.disk_usage(root)
        result.update(total_bytes=usage.total, used_bytes=usage.used, free_bytes=usage.free)
        if not os.access(root, os.R_OK | os.W_OK | os.X_OK):
            raise StorageError("存储目录不可读写，请检查挂载权限", "storage_readonly", 503)
        if usage.free < required_bytes + location.min_free_bytes:
            raise StorageError("存储空间不足，请释放空间后重试", "storage_full", 503)
        result.update(ok=True, code="healthy", message="存储可用")
    except StorageError as exc:
        result.update(code=exc.code, message=exc.message)
    except OSError:
        result.update(code="storage_offline", message="无法读取存储状态，请检查挂载")
    return result


def _require_healthy(location: StorageLocation, required_bytes: int = 0) -> None:
    health = probe_location(location, required_bytes)
    if not health["ok"]:
        raise StorageError(health["message"], health["code"], 503)


def resolve_path(location: StorageLocation, relative_path: str = "", *,
                 must_exist: bool = True, directory: bool | None = None) -> Path:
    """Resolve a relative path after proving root identity and rejecting links."""
    parts = _parts(relative_path)
    root = _root(location)
    target = root.joinpath(*parts)
    try:
        _reject_links(target)
        target.resolve(strict=False).relative_to(root.resolve(strict=True))
        if must_exist and not target.exists():
            raise StorageError("文件或目录不存在", "file_not_found", 404)
        if target.exists() and directory is not None and target.is_dir() != directory:
            raise StorageError("文件类型不匹配", "invalid_file_type")
    except StorageError:
        raise
    except ValueError as exc:
        raise StorageError("文件超出存储范围", "unsafe_path") from exc
    except OSError as exc:
        raise StorageError("无法访问文件或目录", "storage_offline", 503) from exc
    return target


def list_files(user_id: int, location_id: int, path: str = "", *, limit: int = 200) -> dict:
    location = get_location(user_id, location_id)
    parts = _parts(path)
    if _private(parts):
        raise StorageError("此目录仅供下载任务内部使用", "file_not_found", 404)
    limit = _int(limit, "列表条数", minimum=1)
    if limit > 1000:
        raise StorageError("最多列出 1000 项", "invalid_argument")
    root = resolve_path(location, path, directory=True)
    entries = []
    truncated = False
    try:
        # Bound enumeration before sorting to avoid reading huge directories into RAM.
        with os.scandir(root) as children:
            for child in children:
                if _private((child.name,)) or _is_link(Path(child.path)):
                    continue
                if len(entries) >= limit:
                    truncated = True
                    break
                info = child.stat(follow_symlinks=False)
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    continue
                entries.append({
                    "name": child.name, "path": "/".join((*parts, child.name)),
                    "type": "directory" if stat.S_ISDIR(info.st_mode) else "file",
                    "size": info.st_size if stat.S_ISREG(info.st_mode) else None,
                    "modified_at": info.st_mtime,
                })
    except OSError as exc:
        raise StorageError("无法读取存储目录", "storage_offline", 503) from exc
    entries.sort(key=lambda item: (item["type"] != "directory", item["name"].casefold()))
    return {"storage_id": location.id, "path": "/".join(parts), "entries": entries, "truncated": truncated}


def get_file(user_id: int, location_id: int, path: str) -> Path:
    """Return an existing public file; pending attempts and markers stay private."""
    if not path or _private(_parts(path)):
        raise StorageError("文件不存在", "file_not_found", 404)
    result = resolve_path(get_location(user_id, location_id), path, directory=False)
    if not stat.S_ISREG(result.stat().st_mode):
        raise StorageError("此文件类型不能下载", "invalid_file_type")
    return result


def _attempt_paths(task_id: int, attempt_id: int) -> tuple[str, str]:
    task_id = _int(task_id, "任务编号", minimum=1)
    attempt_id = _int(attempt_id, "尝试编号", minimum=1)
    suffix = f"tasks/{task_id}/attempts/{attempt_id}"
    return f"{_PENDING}/{suffix}", f"completed/{suffix}"


def _downloader_path(location: StorageLocation, relative_path: str) -> str:
    base = location.download_path.rstrip("/\\")
    separator = "\\" if "\\" in base and "/" not in base else "/"
    return base + separator + relative_path.replace("/", separator)


def prepare_attempt(location: StorageLocation, task_id: int, attempt_id: int,
                    required_bytes: int = 0) -> dict:
    pending, completed = _attempt_paths(task_id, attempt_id)
    _require_healthy(location, required_bytes)
    target = resolve_path(location, pending, must_exist=False, directory=True)
    if resolve_path(location, completed, must_exist=False).exists():
        raise StorageError("此下载尝试已经归档完成", "attempt_completed", 409)
    try:
        target.mkdir(parents=True, exist_ok=True)
        # Recheck the marker after directory creation, before giving a path out.
        resolve_path(location, pending, directory=True)
    except OSError as exc:
        raise StorageError("无法创建下载目录，请检查挂载及权限", "storage_readonly", 503) from exc
    return {"relative_path": pending, "local_path": str(target),
            "download_path": _downloader_path(location, pending)}


def _manifest(location: StorageLocation, folder: Path, *, hash_files: bool = False) -> list[dict]:
    entries = []
    seen = 0
    def unreadable(_error):
        raise StorageError("下载目录中存在无法读取的文件夹", "storage_offline", 503)
    for current, directories, files in os.walk(folder, followlinks=False, onerror=unreadable):
        _root(location)
        for name in (*directories, *files):
            seen += 1
            if seen > _MAX_TREE_ENTRIES:
                raise StorageError("下载目录中文件数量超过限制", "too_many_files")
            item = Path(current) / name
            if _is_link(item):
                raise StorageError("下载目录中不能包含符号链接", "unsafe_path")
        for name in files:
            item = Path(current) / name
            if name.endswith((".aria2", ".part", ".!qB")):
                raise StorageError("下载目录仍包含未完成文件", "download_incomplete", 409)
            info = item.stat()
            if not stat.S_ISREG(info.st_mode):
                raise StorageError("下载目录包含不支持的文件类型", "unsafe_path")
            row = {"path": item.relative_to(folder).as_posix(), "size": info.st_size}
            _parts(row["path"])
            if hash_files:
                digest = hashlib.sha256()
                with item.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                row["sha256"] = digest.hexdigest()
            entries.append(row)
    if not entries or sum(entry["size"] for entry in entries) == 0:
        raise StorageError("下载目录为空或没有有效内容", "download_empty", 409)
    return sorted(entries, key=lambda row: row["path"])


def _verify_manifest(entries: list[dict], expected_files: list[dict] | None) -> None:
    if expected_files is None:
        return
    if not isinstance(expected_files, list) or not 1 <= len(expected_files) <= _MAX_TREE_ENTRIES:
        raise StorageError("下载器未提供有效文件清单", "invalid_manifest", 409)
    expected = {}
    for item in expected_files:
        if not isinstance(item, dict):
            raise StorageError("下载器文件清单格式无效", "invalid_manifest", 409)
        parts = _parts(item.get("path", ""))
        if not parts or _private(parts):
            raise StorageError("下载器文件清单包含不安全路径", "unsafe_path", 409)
        path = "/".join(parts)
        if path in expected:
            raise StorageError("下载器文件清单包含重复路径", "invalid_manifest", 409)
        expected[path] = _int(item.get("size"), "下载文件大小")
    actual = {item["path"]: item["size"] for item in entries}
    if actual != expected:
        raise StorageError("实际文件与下载器清单不一致，尚未发布下载结果", "download_size_mismatch", 409)


def finalize_attempt(location: StorageLocation, task_id: int, attempt_id: int, *,
                     expected_files: list[dict] | None = None) -> dict:
    """Validate and publish one attempt, without mixing different resources.

Same-filesystem rename is atomic. EXDEV uses a private sibling staging folder,
compares SHA-256 manifests, then atomically publishes the verified copy. Pending
data is retained in the copy case, so interrupted copies never destroy a source.
"""
    pending, completed = _attempt_paths(task_id, attempt_id)
    destination = resolve_path(location, completed, must_exist=False, directory=True)
    if destination.exists():
        entries = _manifest(location, destination)
        _verify_manifest(entries, expected_files)
    else:
        source = resolve_path(location, pending, directory=True)
        entries = _manifest(location, source)
        _verify_manifest(entries, expected_files)
        _require_healthy(location)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            resolve_path(location, completed, must_exist=False, directory=True)
            try:
                source.rename(destination)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                total = sum(entry["size"] for entry in entries)
                _require_healthy(location, total)
                staging = destination.parent / (_COPY_PREFIX + str(attempt_id) + "-" + uuid.uuid4().hex)
                # Never resume an unknown partial copy or follow directory links.
                shutil.copytree(source, staging, symlinks=True)
                source_manifest = _manifest(location, source, hash_files=True)
                copied_manifest = _manifest(location, staging, hash_files=True)
                if source_manifest != copied_manifest:
                    raise StorageError("归档校验失败，原下载文件已保留", "copy_verification_failed", 503)
                _root(location)
                staging.rename(destination)
        except StorageError:
            raise
        except OSError as exc:
            raise StorageError("归档失败，下载文件已保留，请检查存储状态", "storage_write_failed", 503) from exc
        entries = _manifest(location, destination)
        _verify_manifest(entries, expected_files)
    return {"storage_id": location.id, "relative_path": completed,
            "file_count": len(entries), "total_bytes": sum(entry["size"] for entry in entries),
            "files": [{**entry, "path": completed + "/" + entry["path"]} for entry in entries]}
