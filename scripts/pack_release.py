"""把当前代码打成发行目录 release/lingxi/，并可选打 zip。

不含：.venv、.env、data/、测试脚本、旧 deploy/ 快照、__pycache__。
用法（在仓库根或任意目录）：
    python scripts/pack_release.py
    python scripts/pack_release.py --no-zip
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE_DIR = ROOT / "release"
DEST = RELEASE_DIR / "lingxi"
VERSION = "1.2"

SKIP_DIR_NAMES = {
    "__pycache__", ".git", ".venv", "venv", "data", "instance",
    "migrations", ".idea", ".vscode", "deploy", "scripts", "release",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".pyd"}

# 根目录要拷的文件
ROOT_FILES = (
    "wsgi.py",
    "run.py",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    ".dockerignore",
    ".env.example",
    "README.md",
    "LICENSE",
    "NOTICE",
)

# 文档：发行包里只放用户/部署需要的
DOC_FILES = (
    "docs/部署.md",
    "docs/使用说明.html",
    "docs/DEPLOY_BT.md",
)

START_MD = """# 灵犀 发行包 v{version}

本目录是**可部署的发行文件**，不是开发仓库。不含虚拟环境、数据库、密钥和测试。

## 里面有什么

| 路径 | 说明 |
|---|---|
| `app/` | 应用代码 |
| `wsgi.py` | 生产入口（waitress / gunicorn） |
| `run.py` | 仅本地调试 |
| `requirements.txt` | Python 依赖 |
| `.env.example` | 配置模板，复制为 `.env` 后修改 |
| `LICENSE` / `NOTICE` | PolyForm Shield：个人与内部自用可以，不能拿去卖或做竞品 |
| `Dockerfile` / `docker-compose.yml` / `docker/` | Docker 部署 |
| `docs/部署.md` | 宝塔 / 普通 Linux / Docker / MySQL 完整教程 |
| `docs/使用说明.html` | 功能说明（浏览器打开） |

## 最短步骤

1. 安装 Python 3.11/3.12 + MySQL（utf8mb4），建库 `ai_bot`
2. `python -m venv .venv` 后安装依赖：`.venv/bin/pip install -r requirements.txt`
3. `cp .env.example .env`，填写 MySQL
4. `.venv/bin/python -m flask --app wsgi init-db`
5. `.venv/bin/waitress-serve --host=0.0.0.0 --port=8000 --threads=8 wsgi:app`
6. 浏览器打开 `/setup` 或直接登录；每位用户在「设置 → 模型与人设」填自己的 API Key

完整说明见 [docs/部署.md](docs/部署.md)。

> 不要用 uvicorn，不要多 worker。公开网页只开 `PAGE_PORT` 即可，不必配 HTTPS。
"""


def _should_skip(path: Path) -> bool:
    if path.suffix.lower() in SKIP_SUFFIXES:
        return True
    return any(part in SKIP_DIR_NAMES for part in path.parts)


def _copy_tree(src: Path, dst: Path) -> int:
    n = 0
    for item in src.rglob("*"):
        if _should_skip(item.relative_to(src)):
            continue
        if item.is_dir():
            continue
        rel = item.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        n += 1
    return n


def pack(make_zip: bool = True) -> Path:
    if not (ROOT / "wsgi.py").is_file() or not (ROOT / "app").is_dir():
        raise SystemExit(f"未找到应用文件，请在仓库根执行。当前 ROOT={ROOT}")

    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)

    copied = 0
    copied += _copy_tree(ROOT / "app", DEST / "app")
    copied += _copy_tree(ROOT / "docker", DEST / "docker")

    for name in ROOT_FILES:
        src = ROOT / name
        if not src.is_file():
            print(f"  ! 跳过缺失文件：{name}")
            continue
        shutil.copy2(src, DEST / name)
        copied += 1

    for rel in DOC_FILES:
        src = ROOT / rel
        if not src.is_file():
            print(f"  ! 跳过缺失文档：{rel}")
            continue
        target = DEST / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied += 1

    (DEST / "data" / "backups").mkdir(parents=True)
    (DEST / "data" / "images").mkdir(parents=True)
    (DEST / "data" / "backups" / ".gitkeep").write_text("", encoding="utf-8")
    (DEST / "data" / "images" / ".gitkeep").write_text("", encoding="utf-8")
    (DEST / "VERSION").write_text(f"{VERSION}\n", encoding="utf-8")
    (DEST / "开始使用.md").write_text(START_MD.format(version=VERSION), encoding="utf-8")

    zip_path = None
    if make_zip:
        stamp = date.today().strftime("%Y%m%d")
        archive = RELEASE_DIR / f"lingxi-v{VERSION}-{stamp}"
        zip_path = Path(shutil.make_archive(str(archive), "zip", root_dir=DEST.parent, base_dir=DEST.name))

    print(f"发行目录：{DEST}")
    print(f"已复制 {copied} 个文件（另有 VERSION / 开始使用.md / data 占位）")
    if zip_path:
        print(f"压缩包：{zip_path}  ({zip_path.stat().st_size / 1024:.0f} KB)")
    return DEST


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 release/lingxi 发行目录")
    parser.add_argument("--no-zip", action="store_true", help="不打 zip")
    args = parser.parse_args()
    pack(make_zip=not args.no_zip)
    return 0


if __name__ == "__main__":
    sys.exit(main())
