"""图片服务：OpenAI 兼容 images API 封装 + 本地文件存储 + AI 改图。

- 生成：POST {base}/images/generations（优先 b64_json 返回，兼容仅 url 的端点）
- 改图：优先 POST {base}/images/edits（multipart，OpenAI 官方 gpt-image 系列支持）；
  端点不支持时自动降级为「修改要求 + 原图提示词」重新生成新图（parent_id 关联）
- 文件一律落盘到 data/images/，文件名 uuid 化；记录存 image_assets 表
- 配置优先级：settings 表（设置页可改）> .env > 默认值
"""
from __future__ import annotations

import base64
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Optional

import requests

from flask import current_app

from app.extensions import db
from app.models.image import ImageAsset
from app.services.settings_service import get_setting_from
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)

DEFAULT_SIZE = "1024x1024"
_SIZE_RE = re.compile(r"^\d{2,4}x\d{2,4}$")
_TIMEOUT = 300  # 图片生成较慢


class ImageError(Exception):
    """图片生成/编辑失败（信息会反馈给模型或前端）。"""


# ---------- 配置 ----------

def _read_config() -> dict:
    base = str(get_setting_from("image_base_url", "IMAGE_BASE_URL", "") or "").strip().rstrip("/")
    key = str(get_setting_from("image_api_key", "IMAGE_API_KEY", "") or "").strip()
    model = str(get_setting_from("image_model", "IMAGE_MODEL", "") or "").strip()
    size = str(get_setting_from("image_size", "IMAGE_SIZE", DEFAULT_SIZE) or "").strip()
    return {"base_url": base, "api_key": key, "model": model, "size": size or DEFAULT_SIZE}


def is_configured() -> bool:
    cfg = _read_config()
    return bool(cfg["base_url"] and cfg["api_key"] and cfg["model"])


def _image_dir() -> Path:
    d = Path(current_app.config.get("IMAGE_DIR"))
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- 文件保存 ----------

def _sniff_ext(data: bytes) -> str:
    """按魔数识别图片格式，未知默认 png。"""
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.startswith(b"\xff\xd8"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"GIF8"):
        return "gif"
    return "png"


def save_bytes(data: bytes) -> str:
    """图片字节落盘，返回文件名（img-<uuid8>-<时间戳>.<ext>）。"""
    if not data:
        raise ImageError("图片数据为空")
    ext = _sniff_ext(data)
    fname = f"img-{uuid.uuid4().hex[:8]}-{int(time.time()) % 1000000}.{ext}"
    (_image_dir() / fname).write_bytes(data)
    return fname


def _download(url: str) -> bytes:
    """下载端点返回的图片 URL（相对路径补全 base；仅允许公网地址，防 SSRF）。"""
    from app.utils.urlsafety import safe_get, validate_public_url

    base = _read_config()["base_url"]
    if url.startswith("/") and base:
        url = base + url
    try:
        url = validate_public_url(url)
    except ValueError as e:
        raise ImageError(f"图片地址非公网：{e}") from e
    try:
        # safe_get：手动逐跳重定向，每跳请求前校验公网（杜绝 blind SSRF）
        resp = safe_get(url, timeout=120)
    except (requests.RequestException, ValueError) as e:
        raise ImageError(f"下载图片失败：{str(e)[:150]}") from e
    if resp.status_code != 200:
        raise ImageError(f"下载图片失败 HTTP {resp.status_code}")
    if not (resp.headers.get("Content-Type") or "").startswith("image/"):
        raise ImageError("下载内容不是图片")
    return resp.content


# ---------- API 调用 ----------

def _api_headers() -> dict:
    return {"Authorization": f"Bearer {_read_config()['api_key']}"}


def _parse_item(resp: requests.Response) -> dict:
    """解析 images API 响应中的第一个图片对象。"""
    try:
        data = resp.json()
    except ValueError:
        raise ImageError(f"接口返回非 JSON：{resp.text[:200]}") from None
    if resp.status_code >= 400 or data.get("error"):
        msg = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) \
            else str(data.get("error") or "")
        raise ImageError(msg or f"接口错误 HTTP {resp.status_code}"[:300])
    items = data.get("data") or []
    if not items:
        raise ImageError("接口未返回图片数据")
    return items[0]


def _generate_bytes(prompt: str, size: str, model: str) -> bytes:
    """调用 generations 接口生成图片字节。优先 b64_json，兼容仅 url 端点。"""
    cfg = _read_config()
    payload = {"model": model, "prompt": prompt, "size": size, "n": 1,
               "response_format": "b64_json"}
    resp = requests.post(f"{cfg['base_url']}/images/generations",
                         json=payload, headers=_api_headers(), timeout=_TIMEOUT)
    # 端点不支持 b64_json（部分兼容实现）→ 降级重试 url 模式
    if resp.status_code == 400 and "b64" in resp.text.lower():
        payload.pop("response_format", None)
        resp = requests.post(f"{cfg['base_url']}/images/generations",
                             json=payload, headers=_api_headers(), timeout=_TIMEOUT)
    item = _parse_item(resp)
    if item.get("b64_json"):
        try:
            return base64.b64decode(item["b64_json"])
        except (ValueError, TypeError):
            raise ImageError("接口返回的 b64_json 无法解码") from None
    if item.get("url"):
        return _download(item["url"])
    raise ImageError("接口返回中既无 b64_json 也无 url")


def generate(user_id: int, prompt: str, size: Optional[str] = None,
             model: Optional[str] = None) -> ImageAsset:
    """生成图片：调 API → 落盘 → 入库，返回 ImageAsset。"""
    cfg = _read_config()
    if not is_configured():
        raise ImageError("图片生成未配置：请先在「设置 → 图片生成」填入 BaseURL / API Key / 模型")
    prompt = (prompt or "").strip()
    if not prompt:
        raise ImageError("提示词不能为空")
    size = (size or cfg["size"]).strip()
    if not _SIZE_RE.match(size):
        raise ImageError("size 格式不正确，应为 宽x高，如 1024x1024")
    model = (model or cfg["model"]).strip()

    data = _generate_bytes(prompt, size, model)
    fname = save_bytes(data)
    asset = ImageAsset(user_id=user_id, prompt=prompt, model=model, size=size, file_path=fname)
    db.session.add(asset)
    db.session.commit()
    return asset


def edit(asset: ImageAsset, instruction: str) -> ImageAsset:
    """AI 改图：优先走 /images/edits 编辑接口，不支持则降级为按描述重新生成。

    两种路径都会生成一张新图（parent_id 指向原图，instruction 记录修改要求）。
    """
    cfg = _read_config()
    if not is_configured():
        raise ImageError("图片生成未配置：请先在「设置 → 图片生成」填入 BaseURL / API Key / 模型")
    instruction = (instruction or "").strip()
    if not instruction:
        raise ImageError("修改要求不能为空")

    edited = _try_edit(asset, instruction)
    if edited is None:
        # 降级：原提示词 + 修改要求，重新生成新图
        prompt = f"{instruction}。参考原图生成提示词：{asset.prompt or '（无）'}"
        new_asset = generate(asset.user_id, prompt, size=asset.size)
        new_asset.instruction = instruction
        new_asset.parent_id = asset.id
        db.session.commit()
        return new_asset
    return edited


def _try_edit(asset: ImageAsset, instruction: str) -> Optional[ImageAsset]:
    """调用 /images/edits（multipart）。端点不存在/不支持时返回 None（触发降级）。"""
    cfg = _read_config()
    path = _image_dir() / asset.file_path
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            resp = requests.post(
                f"{cfg['base_url']}/images/edits",
                data={"model": cfg["model"], "prompt": instruction, "size": asset.size or cfg["size"]},
                files={"image": (asset.file_path, fh, "image/png")},
                headers=_api_headers(),
                timeout=_TIMEOUT,
            )
    except requests.RequestException as e:
        logger.warning("图片编辑接口请求失败，降级重生成: %s", e)
        return None
    if resp.status_code in (404, 405, 501) or (resp.status_code == 400 and "model" in resp.text.lower()):
        logger.info("图片编辑接口不可用（HTTP %s），降级重生成", resp.status_code)
        return None
    try:
        item = _parse_item(resp)
    except ImageError:
        return None
    data = b""
    try:
        if item.get("b64_json"):
            data = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            data = _download(item["url"])
    except (ValueError, TypeError):
        data = b""
    if not data:
        return None
    fname = save_bytes(data)
    new_asset = ImageAsset(user_id=asset.user_id, prompt=asset.prompt, instruction=instruction,
                           model=cfg["model"], size=asset.size or cfg["size"], file_path=fname,
                           parent_id=asset.id)
    db.session.add(new_asset)
    db.session.commit()
    return new_asset


# ---------- 查询 / 管理 ----------

def get_image(image_id, user_id: Optional[int] = None) -> Optional[ImageAsset]:
    """按 id 取未删除图片，非本用户返回 None。"""
    try:
        image_id = int(image_id)
    except (TypeError, ValueError):
        return None
    asset = db.session.get(ImageAsset, image_id)
    if asset is None or asset.deleted_at is not None:
        return None
    if user_id is not None and asset.user_id != user_id:
        return None
    return asset


def get_by_filename(filename: str) -> Optional[ImageAsset]:
    """按文件名取未删除图片（供 /img/ 文件访问路由）。"""
    return ImageAsset.query.filter(
        ImageAsset.file_path == filename,
        ImageAsset.deleted_at.is_(None),
    ).first()


def list_images(user_id: int, limit: int = 200) -> list[ImageAsset]:
    return (ImageAsset.query.filter(ImageAsset.deleted_at.is_(None),
                                    ImageAsset.user_id == user_id)
            .order_by(ImageAsset.created_at.desc()).limit(limit).all())


def soft_delete(asset: ImageAsset) -> None:
    """软删除记录（磁盘文件保留，可回滚）。"""
    asset.deleted_at = utcnow()
    db.session.commit()


def set_public(asset: ImageAsset, is_public: bool) -> ImageAsset:
    asset.is_public = bool(is_public)
    db.session.commit()
    return asset


def asset_url(asset: ImageAsset) -> str:
    """图片绝对访问地址（/img/<文件名>）。"""
    from flask import request

    return f"{request.host_url.rstrip('/')}/img/{asset.file_path}"
