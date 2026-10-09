"""Persistent media jobs; database is the queue and event replay ledger."""
from sqlalchemy import UniqueConstraint

from app.extensions import db
from app.utils.timeutil import utcnow


class DownloadTask(db.Model):
    __tablename__ = "media_download_tasks"
    __table_args__ = (UniqueConstraint("user_id", "request_key", name="uq_media_request"),)
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    storage_id = db.Column(db.Integer, nullable=False)
    conversation_id = db.Column(db.Integer, nullable=False)
    request_key = db.Column(db.String(64), nullable=False)
    requirement = db.Column(db.JSON, nullable=False)
    state = db.Column(db.String(32), nullable=False, default="searching", index=True)
    control = db.Column(db.String(16), nullable=False, default="")
    resume_state = db.Column(db.String(32), nullable=False, default="searching")
    policy = db.Column(db.JSON, nullable=False, default=dict)
    recovery_count = db.Column(db.Integer, nullable=False, default=0)
    current_attempt_id = db.Column(db.Integer)
    agent_run_id = db.Column(db.Integer)
    error = db.Column(db.String(500), nullable=False, default="")
    result = db.Column(db.JSON, nullable=False, default=dict)
    next_run_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    lease_token = db.Column(db.String(32), nullable=True)
    lease_until = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class ResourceCandidate(db.Model):
    __tablename__ = "media_resource_candidates"
    __table_args__ = (UniqueConstraint("task_id", "fingerprint", name="uq_media_candidate"),)
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    task_id = db.Column(db.Integer, nullable=False, index=True)
    fingerprint = db.Column(db.String(160), nullable=False)
    data = db.Column(db.JSON, nullable=False)
    rejected = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class DownloadAttempt(db.Model):
    __tablename__ = "media_download_attempts"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, nullable=False, index=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    candidate_id = db.Column(db.Integer, nullable=False)
    attempt_key = db.Column(db.String(32), nullable=False, unique=True)
    downloader_kind = db.Column(db.String(20), nullable=False)
    external_id = db.Column(db.String(128), nullable=False, default="")
    state = db.Column(db.String(32), nullable=False, default="submitting")
    retry_count = db.Column(db.Integer, nullable=False, default=0)
    infrastructure_paused = db.Column(db.Boolean, nullable=False, default=False)
    downloaded_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    total_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    speed = db.Column(db.BigInteger, nullable=False, default=0)
    last_progress_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    paths = db.Column(db.JSON, nullable=False, default=dict)
    error = db.Column(db.String(500), nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class MediaEvent(db.Model):
    __tablename__ = "media_events"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    task_id = db.Column(db.Integer, nullable=False, index=True)
    kind = db.Column(db.String(40), nullable=False)
    data = db.Column(db.JSON, nullable=False, default=dict)
    notify_pending = db.Column(db.Boolean, nullable=False, default=False, index=True)
    notify_lease_until = db.Column(db.DateTime)
    notify_attempts = db.Column(db.Integer, nullable=False, default=0)
    notify_channels = db.Column(db.JSON)
    notified_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
