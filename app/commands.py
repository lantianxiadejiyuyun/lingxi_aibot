"""flask CLI 命令：init-db / create-admin / reset-db / reset-admin-password / 备份 / reindex / routes。"""
from __future__ import annotations

import click
from flask import current_app
from flask.cli import with_appcontext

from app.extensions import db


@click.command("media-worker")
@click.option("--once", is_flag=True, help="处理一次队列后退出")
@click.option("--concurrency", default=3, type=click.IntRange(1, 8), help="并行处理任务数")
@with_appcontext
def media_worker(once, concurrency):
    """运行持久下载任务队列；复用应用配置，不启动飞书或定时调度器。"""
    from concurrent.futures import ThreadPoolExecutor
    import signal
    import threading
    from app.services.media_service import process_one, deliver_notifications
    from app.services.native_agent_service import process_queued_agent

    app = current_app._get_current_object()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    def work():
        while not stop.is_set():
            with app.app_context():
                try:
                    progressed = process_one()
                    if not progressed:
                        progressed = process_queued_agent()
                except Exception as exc:
                    db.session.rollback()
                    app.logger.error("下载 worker 暂不可用：%s", type(exc).__name__)
                    progressed = False
            if once:
                return
            if not progressed:
                stop.wait(2)

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="media-worker") as executor:
        futures = [executor.submit(work) for _ in range(concurrency)]
        while not stop.is_set():
            with app.app_context():
                try:
                    from app.services.settings_service import set_setting
                    from app.utils.timeutil import utcnow
                    set_setting("media_worker_heartbeat", utcnow().isoformat(), user_id=0)
                    deliver_notifications()
                except Exception as exc:
                    db.session.rollback()
                    app.logger.error("下载通知暂不可用：%s", type(exc).__name__)
            if once:
                for future in futures:
                    future.result()
                break
            stop.wait(5)


@click.command("init-db")
@with_appcontext
def init_db():
    """建表 + 补列 + 初始化管理员/默认设置/内置定时任务（幂等）。"""
    from app.services.install_service import initialize_database

    for msg in initialize_database(current_app._get_current_object()):
        click.echo(msg)


@click.command("sync-daily-reports")
@click.option("--user-id", type=click.IntRange(min=1), help="仅同步指定账号；省略则同步全部账号")
@with_appcontext
def sync_daily_reports(user_id):
    """把已有早午晚简报归档到日历；可重复执行，不调用模型或发送通知。"""
    from app.models.user import User
    from app.services.daily_report_service import backfill_history

    query = User.query.order_by(User.id)
    if user_id is not None:
        query = query.filter(User.id == user_id)
    users = query.all()
    if user_id is not None and not users:
        raise click.ClickException("账号不存在")
    count = sum(backfill_history(user) for user in users)
    click.echo(f"✓ 已检查 {len(users)} 个账号，补入 {count} 段历史简报")


@click.command("reset-db")
@click.option("--yes", "confirmed", is_flag=True, help="确认删除当前库全部数据表")
@with_appcontext
def reset_db(confirmed):
    """DROP 当前库全部数据表（不删库、不改 .env）。未加 --yes 只列出表名。"""
    from sqlalchemy import inspect

    try:
        names = list(inspect(db.engine).get_table_names())
    except Exception as e:  # noqa: BLE001
        raise click.ClickException(f"无法连接数据库：{e}") from e
    if not names:
        click.echo("当前库没有数据表")
        return
    click.echo("将删除以下数据表：")
    for name in names:
        click.echo(f"  - {name}")
    if not confirmed:
        raise click.ClickException(
            "未执行删除。确认请加上 --yes：python -m flask reset-db --yes")
    from app.services.install_service import drop_all_tables

    dropped = drop_all_tables()
    click.echo(f"✓ 已删除 {len(dropped)} 张数据表")


@click.command("reset-admin-password")
@click.option("--username", default="", help="管理员用户名；库中仅一名管理员时可省略")
@click.option("--password", default=None, help="新密码；省略则随机生成并打印一次")
@with_appcontext
def reset_admin_password(username, password):
    """重设管理员登录密码（密码只存哈希，无法找回明文）。"""
    import secrets

    from app.models.user import User

    username = (username or "").strip()
    admins = User.query.filter_by(is_admin=True).order_by(User.id.asc()).all()
    if username:
        user = User.query.filter_by(username=username).first()
        if user is None:
            raise click.ClickException(f"用户 {username} 不存在")
        if not user.is_admin:
            raise click.ClickException(f"用户 {username} 不是管理员")
    elif len(admins) == 1:
        user = admins[0]
    elif not admins:
        raise click.ClickException("库中没有管理员账号")
    else:
        names = ", ".join(a.username for a in admins)
        raise click.ClickException(f"有多名管理员，请指定 --username。现有：{names}")

    generated = False
    if password is None:
        password = secrets.token_urlsafe(12)
        generated = True
    if len(password or "") < 6:
        raise click.ClickException("密码至少 6 位")
    user.set_password(password)
    db.session.commit()
    click.echo(f"✓ 已重置管理员 {user.username} 的密码")
    if generated:
        click.echo(f"随机密码（只显示一次）：{password}")


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
    admin = User(username=username, timezone=current_app.config["APP_TIMEZONE"], is_admin=True)
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
