"""flask CLI 命令。"""
from __future__ import annotations

import click
from flask import current_app
from flask.cli import with_appcontext

from app.extensions import db


@click.command("init-db")
@with_appcontext
def init_db():
    """建表 + 补列 + 初始化管理员/默认设置/内置定时任务（幂等）。"""
    from app.services.install_service import initialize_database

    for msg in initialize_database(current_app._get_current_object()):
        click.echo(msg)


@click.command("create-admin")
@click.option("--username", prompt=True)
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
@with_appcontext
def create_admin(username, password):
    """手动创建管理员。"""
    from app.models.user import User

    username = (username or "").strip()
    if not username:
        raise click.ClickException("用户名不能为空")
    if len(password or "") < 6:
        raise click.ClickException("密码至少 6 位")
    if User.query.filter_by(username=username).first():
        raise click.ClickException("用户名已存在")
    admin = User(username=username, timezone=current_app.config["APP_TIMEZONE"])
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()
    click.echo(f"✓ 已创建用户：{username}")


@click.command("backup-now")
@with_appcontext
def backup_now():
    """立即执行一次数据备份。"""
    from app.services.backup_service import backup_to_json

    path = backup_to_json()
    click.echo(f"✓ 备份完成：{path}")


@click.command("restore-backup")
@click.argument("path")
@with_appcontext
def restore_backup_cmd(path):
    """从备份文件恢复数据（支持 .json / .json.gz）。"""
    from app.services.backup_service import restore_from_json

    restored = restore_from_json(path)
    total = sum(restored.values())
    click.echo(f"✓ 已恢复 {len(restored)} 张表，共 {total} 条记录")


@click.command("reindex")
@with_appcontext
def reindex():
    """重建 RAG 语义检索向量索引（未配置嵌入服务时提示）。"""
    from app.services import rag_service

    if not rag_service.is_configured():
        click.echo("未配置嵌入服务（EMBEDDING_BASE_URL / API_KEY / MODEL），跳过重建")
        return
    from app.models.conversation import Conversation
    from app.models.note import Note
    from app.models.webpage import WebPage

    count = 0
    for n in Note.query.filter(Note.deleted_at.is_(None)).all():
        rag_service.index_text("note", n.id, f"{n.title}\n{n.content}")
        count += 1
    for p in WebPage.query.filter(WebPage.deleted_at.is_(None)).all():
        rag_service.index_text("webpage", p.id,
                               f"{p.title}\n{p.description}\n{(p.content or '')[:3000]}")
        count += 1
    for c in Conversation.query.all():
        text = c.summary or "\n".join(
            m.content for m in c.messages if m.role in ("user", "assistant") and m.content)
        if text.strip():
            rag_service.index_text("conversation", c.id, text)
            count += 1
    click.echo(f"✓ 已重建 {count} 条向量索引")


@click.command("routes")
@with_appcontext
def list_routes():
    """列出全部路由（调试用）。"""
    for rule in sorted(current_app.url_map.iter_rules(), key=lambda r: r.rule):
        click.echo(f"{rule.rule:40s} {','.join(sorted(rule.methods - {'HEAD', 'OPTIONS'}))}")
