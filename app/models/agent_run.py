"""Persistent execution ledger for Lingxi's own bounded tool agents."""
from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.utils.timeutil import utcnow


class AgentRun(db.Model):
    __tablename__ = "agent_runs"
    __table_args__ = (UniqueConstraint("user_id", "task_key", name="uq_agent_run_task_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parent_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    root_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    task_key: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(20), default="coordinator", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False, index=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_tools: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    checkpoint: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[str] = mapped_column(Text().with_variant(mysql.LONGTEXT(), "mysql"), default="", nullable=False)
    error: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    max_rounds: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    max_tool_calls: Mapped[int] = mapped_column(Integer, default=24, nullable=False)
    max_children: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    # These counters live on the root and are atomically consumed by every child.
    rounds_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tools_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    children_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    local_rounds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(default=False, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentStep(db.Model):
    __tablename__ = "agent_steps"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_agent_step_sequence"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    tool_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    call_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    arguments: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[str] = mapped_column(Text().with_variant(mysql.LONGTEXT(), "mysql"), default="", nullable=False)
    read_only: Mapped[bool] = mapped_column(default=False, nullable=False)
    child_run_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
