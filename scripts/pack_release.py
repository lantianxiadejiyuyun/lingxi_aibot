"""Create a Rust/Vue source release, excluding credentials and runtime data."""
from __future__ import annotations
import argparse
import shutil
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE_DIR = ROOT / "release"
VERSION = "2.0.0"
SKIP = {"node_modules", "dist", "target", "__pycache__", ".git", ".venv"}
FILES = ["Cargo.toml", "Cargo.lock", "Dockerfile", "docker-compose.yml",
         "docker-compose.quickstart.yml", ".dockerignore", "README.md", "LICENSE", "NOTICE",
         "scripts/export_legacy.py", "scripts/requirements-migration.txt", "scripts/pack_release.py",
         "docs/Rust部署与迁移.md", "docs/Rust-Vue架构.md", "docs/legacy/README.md",
         ".env.example", ".env.docker.example", "docker-compose.astrbot.yml",
         "docker-compose.fnos.yml", "docker-compose.ollama.yml", "docker-compose.feishu.yml", "docs/飞书私聊.md", "docs/fnOS部署.md", "docs/B站书房.md"]

def pack(make_zip: bool = True) -> Path:
    if not (ROOT / "backend/Cargo.toml").is_file() or not (ROOT / "web/package-lock.json").is_file():
        raise SystemExit("Rust/Vue source or lockfile is missing")
    RELEASE_DIR.mkdir(exist_ok=True)
    stem = f"lingxi-v{VERSION}-{date.today():%Y%m%d}"
    destination = RELEASE_DIR / stem
    number = 1
    while destination.exists() or destination.with_suffix(".zip").exists():
        destination = RELEASE_DIR / f"{stem}-{number}"
        number += 1
    destination.mkdir()
    copied = 0
    for folder in ("backend", "web", "integrations/astrbot", "integrations/feishu"):
        for source in (ROOT / folder).rglob("*"):
            relative = source.relative_to(ROOT)
            if not source.is_file() or any(part in SKIP for part in relative.parts) or source.name.startswith(".env"):
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied += 1
    for name in FILES:
        source = ROOT / name
        if source.is_file():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied += 1
    (destination / "VERSION").write_text(VERSION + "\n", encoding="utf-8")
    (destination / "开始使用.md").write_text("# 灵犀 Rust / Vue 2.0\n\n请从 [部署与迁移](docs/Rust部署与迁移.md) 开始。发行包是源码包，由 Docker 构建；不含密码、模型密钥或运行数据。旧版回退需要保留完整旧部署及备份。\n", encoding="utf-8")
    if make_zip:
        archive = shutil.make_archive(str(destination), "zip", root_dir=destination.parent, base_dir=destination.name)
        print(f"Archive: {archive}")
    print(f"Release: {destination} ({copied} source files)")
    return destination

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-zip", action="store_true")
    arguments = parser.parse_args()
    pack(make_zip=not arguments.no_zip)
