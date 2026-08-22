"""数据管理：一键重置业务数据（保留登录账号与系统配置）。

重置范围：
    - 清空业务数据表：日程/任务/笔记/对话/通知/网页/图片/技能/记忆/向量/健身/出行/消费
    - 删除自定义定时提醒（scheduled_jobs.is_builtin=0），保留内置任务并重置其运行历史
    - 清理磁盘上生成的图片文件（data/images）
    - 保留：users（账号）、settings（系统配置）、内置定时任务、备份文件
"""
from __future__ import annotations

import shutil
from pathlib import Path

from flask import current_app
from sqlalchemy import text

from app.extensions import db

# 需清空的业务数据表（注意 messages 需在 conversations 之前删除以应对外键）
BUSINESS_TABLES = [
    "messages",
    "conversations",
    "events",
    "tasks",
    "notes",
    "notifications",
    "webpages",
    "image_assets",
    "skills",
    "memories",
    "embeddings",
    "fitness_records",
    "trip_plans",
    "expense_records",
]


def _set_fk_checks(enabled: bool) -> None:
    """MySQL 下开关外键检查；非 MySQL 忽略。"""
    if db.engine.dialect.name != "mysql":
        return
    db.session.execute(text("SET FOREIGN_KEY_CHECKS = %d" % (1 if enabled else 0)))


def reset_all_data() -> dict[str, int]:
    """清空全部业务数据，返回各表删除行数。

    任何一步失败都会回滚（数据不受影响），并向外抛出异常。
    """
    from sqlalchemy import inspect as sa_inspect

    deleted: dict[str, int] = {}
    is_mysql = db.engine.dialect.name == "mysql"
    existing = set(sa_inspect(db.engine).get_table_names())
    if is_mysql:
        db.session.execute(text("SET FOREIGN_KEY_CHECKS = 0"))

    try:
        for table in BUSINESS_TABLES:
            if table not in existing:
                deleted[table] = 0
                continue
            deleted[table] = db.session.execute(text(f"DELETE FROM `{table}`")).rowcount

        deleted["scheduled_jobs_custom"] = db.session.execute(
            text("DELETE FROM scheduled_jobs WHERE is_builtin = 0")
        ).rowcount
        db.session.execute(
            text("UPDATE scheduled_jobs SET last_run_utc = NULL, last_status = '' WHERE is_builtin = 1")
        )

        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    finally:
        if is_mysql:
            db.session.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
            db.session.commit()

    _clean_image_dir()
    return deleted


def _clean_image_dir() -> None:
    """清空生成图片目录下的文件（保留目录本身，忽略失败）。"""
    image_dir = Path(current_app.config["IMAGE_DIR"])
    if not image_dir.exists():
        return
    for p in image_dir.iterdir():
        try:
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
