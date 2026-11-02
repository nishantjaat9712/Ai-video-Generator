from __future__ import annotations

from datetime import datetime, timezone
import uuid
from sqlalchemy import String, Text, JSON, Integer, Float, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base

def uid() -> str:
    return uuid.uuid4().hex

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)

class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    path: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(20))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now)

class Character(Base):
    __tablename__ = "characters"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now)

class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(100), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    logs: Mapped[list] = mapped_column(JSON, default=list)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    output_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)

class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_seen: Mapped[float] = mapped_column(Float)

class Usage(Base):
    __tablename__ = "usage"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    request_id: Mapped[str] = mapped_column(String(200))
    duration_seconds: Mapped[float] = mapped_column(Float, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
