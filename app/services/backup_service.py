"""数据备份：全表流式导出为 gzip 压缩的 JSON 文件，可配置保留份数，并注册调度动作 data_backup。

针对大数据量做了两点优化：
    1. 流式写出：逐表、逐行写入文件，绝不把全库载入内存（避免 OOM）。
    2. gzip 压缩：JSON 文本压缩比很高（通常 10~20 倍），显著减小磁盘占用与下载体积。
    配合 MySQL 服务端游标（stream_results）与单事务一致性快照，超大表也能稳定备份。

配置（设置页「备份设置」，settings 表，缺省回退默认值）：
    backup_time    - 每日执行时间 HH:MM（同步内置任务 cron）
    backup_enabled - 是否启用自动备份（同步内置任务 enabled）
    backup_keep    - 保留备份份数（默认 7）
    backup_channel - 完成通知渠道（默认 ["inapp"]）

备份文件格式（gzip 解压后为标准 JSON）：
    {"exported_at": "YYYY-MM-DD HH:MM:SS", "tables": {表名: [行 dict...]}}
datetime / date / Decimal 等类型统一 str() 转字符串。
"""
from __future__ import annotations

import gzip
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from flask import current_app
from sqlalchemy import MetaData, Table, inspect, select, text

from app.extensions import db
from app.scheduler import register_action
from app.services.settings_service import get_setting
from app.utils.timeutil import utcnow

KEEP_BACKUPS = 7  # 保留最近 N 份备份（可被 settings.backup_keep 覆盖）

# 支持的备份文件后缀（新文件为 .json.gz，兼容历史 .json）
_SUFFIXES = (".json", ".json.gz")

# 恢复表白名单：仅业务表、仅合法标识符，防反引号注入与任意表覆写
_TABLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_BUSINESS_TABLES = frozenset({
    "users", "events", "tasks", "notes", "conversations", "messages",
    "notifications", "scheduled_jobs", "settings", "webpages", "image_assets",
    "skills", "memories", "embeddings", "fitness_records", "trip_plans",
    "expense_records",
    "storage_locations", "media_download_tasks", "media_download_attempts",
    "media_resource_candidates", "media_events", "agent_runs", "agent_steps",
})


def _serialize(value):
    """datetime / date / Decimal → str()，其余原样返回。"""
    if isinstance(value, (datetime, date, Decimal)):
        return str(value)
    return value


def _keep_count() -> int:
    try:
        return max(1, min(30, int(get_setting("backup_keep") or KEEP_BACKUPS)))
    except (TypeError, ValueError):
        return KEEP_BACKUPS


def _backup_files(backup_dir: Path) -> list[Path]:
    """备份目录下所有备份文件（含 .json 与 .json.gz），按修改时间升序。"""
    if not backup_dir.exists():
        return []
    files = []
    for pattern in ("backup-*.json", "backup-*.json.gz"):
        files.extend(backup_dir.glob(pattern))
    return sorted(files, key=lambda p: p.stat().st_mtime)


def backup_to_json() -> Path:
    """流式导出全部数据表为 gzip 压缩的 JSON 文件，保留最近 N 份。

    - 单事务内逐表读取（InnoDB 一致性快照）
    - 逐行写入 gzip 流，内存占用与单行大小相当，与库总量无关
    :return: 本次备份文件的 Path（.json.gz）
    """
    backup_dir = Path(current_app.config["BACKUP_DIR"])
    backup_dir.mkdir(parents=True, exist_ok=True)

    inspector = inspect(db.engine)
    meta = MetaData()
    now = utcnow()
    filename = f"backup-{now.strftime('%Y%m%d-%H%M%S')}-{now.microsecond:06d}.json.gz"
    path = backup_dir / filename

    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write('{"exported_at": ')
        fh.write(json.dumps(str(now), ensure_ascii=False))
        fh.write(', "tables": {')
        first_table = True
        for table_name in inspector.get_table_names():
            table = Table(table_name, meta, autoload_with=db.engine)
            if not first_table:
                fh.write(", ")
            fh.write(json.dumps(table_name, ensure_ascii=False))
            fh.write(": [")
            first_table = False

            first_row = True
            result = db.session.execute(
                select(table), execution_options={"stream_results": True}
            ).mappings()
            for row in result:
                item = {key: _serialize(value) for key, value in row.items()}
                if not first_row:
                    fh.write(", ")
                fh.write(json.dumps(item, ensure_ascii=False, default=str))
                first_row = False
            fh.write("]")
        fh.write("}}")

    # 释放只读事务（流式游标结束后的收尾）
    db.session.rollback()

    # 保留最近 N 份，删除更早的
    backups = _backup_files(backup_dir)
    for old in backups[:-_keep_count()]:
        try:
            old.unlink()
        except OSError:
            pass

    return path


def list_backups() -> list[dict]:
    """最近备份文件列表（新→旧）：name / size(字节) / mtime(naive UTC，展示需转时区)。"""
    backup_dir = Path(current_app.config["BACKUP_DIR"])
    rows = []
    for p in _backup_files(backup_dir)[::-1]:
        st = p.stat()
        rows.append({
            "name": p.name,
            "size": st.st_size,
            # 统一 naive UTC（datetime.fromtimestamp 不带 tz 会取服务器本地时区）
            "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).replace(tzinfo=None),
        })
    return rows


def delete_backup(name: str) -> None:
    """删除一个备份文件（严格校验文件名，防止路径穿越）。"""
    backup_dir = Path(current_app.config["BACKUP_DIR"])
    p = (backup_dir / name).resolve()
    if (p.parent != backup_dir.resolve()
            or not p.name.startswith("backup-")
            or not p.name.endswith(_SUFFIXES)):
        raise ValueError("非法的备份文件名")
    if p.exists():
        p.unlink()


def _restore_users(table, rows: list) -> int:
    """恢复 users：不整表清空，不覆盖已有账号，新增用户强制非管理员。"""
    from app.models.user import User

    existing_ids = {u.id for u in User.query.all()}
    existing_names = {u.username for u in User.query.all()}
    inserted = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        uid = row.get("id")
        name = row.get("username")
        if uid in existing_ids or (name and name in existing_names):
            continue  # 不覆盖已有用户（含管理员）
        payload = dict(row)
        payload["is_admin"] = False
        db.session.execute(table.insert(), [payload])
        if uid is not None:
            existing_ids.add(uid)
        if name:
            existing_names.add(name)
        inserted += 1
    return inserted


def restore_from_json(path) -> dict[str, int]:
    """从备份文件恢复数据：逐表清空后按备份重建（保留原 ID），返回各表恢复行数。

    - 兼容 .json 与 .json.gz（自动识别）
    - MySQL 下关闭外键检查后再写，任何一步失败整体回滚
    - 表在备份中存在但当前库不存在时跳过（避免旧备份破坏新表）
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"备份文件不存在：{path}")

    if path.name.endswith(".json.gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))

    tables = payload.get("tables") or {}
    if not isinstance(tables, dict):
        raise ValueError("备份文件格式不正确：缺少 tables 字段")

    inspector = inspect(db.engine)
    existing = set(inspector.get_table_names())
    meta = MetaData()

    restored: dict[str, int] = {}
    is_mysql = db.engine.dialect.name == "mysql"
    if is_mysql:
        db.session.execute(text("SET FOREIGN_KEY_CHECKS = 0"))

    try:
        for table_name, rows in tables.items():
            if not isinstance(table_name, str) or not _TABLE_NAME_RE.fullmatch(table_name):
                continue
            if (table_name not in existing or table_name not in _BUSINESS_TABLES
                    or not isinstance(rows, list)):
                continue
            table = Table(table_name, meta, autoload_with=db.engine)
            if table_name == "settings":
                continue  # 不覆盖运行时配置（含密钥）
            if table_name == "users":
                restored[table_name] = _restore_users(table, rows)
                continue
            db.session.execute(text(f"DELETE FROM `{table_name}`"))
            # 分批插入，避免单条多值语句过大
            batch = 1000
            for i in range(0, len(rows), batch):
                chunk = rows[i:i + batch]
                if chunk:
                    db.session.execute(table.insert(), chunk)
            restored[table_name] = len(rows)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    finally:
        if is_mysql:
            db.session.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
            db.session.commit()

    return restored


@register_action("data_backup", description="导出全部数据表为 JSON 备份（参数：无）")
def run_backup(user, params: dict | None = None) -> None:
    """调度动作：仅管理员执行全量备份，并按场景渠道发送完成通知。"""
    from app.services.notify_service import notify_for

    if not getattr(user, "is_admin", False):
        return  # 普通用户不触发全量备份（避免备份到他人数据）

    path = backup_to_json()
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    table_count = len(data.get("tables", {}))
    notify_for("backup", "🗄️ 数据备份完成",
               f"备份文件：{path.name}，共 {table_count} 张表", user_id=user.id)
