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
    "docker-compose.quickstart.yml",
    ".dockerignore",
    ".env.example",
    ".env.docker.example",
    "README.md",
    "LICENSE",
    "NOTICE",
)

# 保留 README 导航涉及的文档，使解压后的文档链接仍可用。
DOC_FILES = (
    "release/README.md",
    "docs/部署.md",
    "docs/部署参考.md",
    "docs/使用指南.md",
    "docs/开发指南.md",
    "docs/更新记录.md",
    "docs/PROJECT.md",
    "docs/PLAN.md",
    "docs/DEPLOY_BT.md",
    "docs/使用说明.html",
)

START_MD = """# 灵犀 发行包 v{version}

解压到独立目录后，推荐使用 **Docker** 安装。安装包包含应用、配置模板和文档；不含密钥、运行数据或测试环境。

## 首次安装

按 [Docker 首次安装](docs/部署.md#首次安装) 操作：准备 Docker，将 `.env.docker.example` 复制为 `.env` 并填写两个 MySQL 密码，再按顺序启动数据库、初始化管理员、启动应用。无需域名，也无需单独安装 Python 或 MySQL。

登录后按 [使用指南](docs/使用指南.md) 配置自己的 AI Key、飞书和网页分享地址。

## 升级已有安装

先备份，再按 [升级步骤](docs/部署.md#升级) 替换程序。保留原 `.env`、`data/`、数据库卷和实际使用的 Compose 配置，不要用模板覆盖。

宝塔、已有 MySQL 或原生 Python 部署见 [部署参考](docs/部署参考.md)；本地开发请使用完整仓库并阅读 [开发指南](docs/开发指南.md)。版本变化见 [更新记录](docs/更新记录.md)。
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
