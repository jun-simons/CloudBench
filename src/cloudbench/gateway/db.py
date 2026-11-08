"""Database models and engine setup (SQLite in WAL mode via aiosqlite)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, event
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from cloudbench.protocol import RunState


class UTCDateTime(TypeDecorator[datetime]):
    """Stores timezone-aware UTC datetimes; SQLite would otherwise drop tzinfo."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime stored in UTCDateTime column")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this class attribute
        datetime: UTCDateTime,
        dict[str, Any]: JSON,
    }


class PrincipalKind(StrEnum):
    GUEST = "guest"


class Principal(Base):
    """Someone using the lab: a human guest session or, later, an API key holder."""

    __tablename__ = "principals"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime]


class SessionToken(Base):
    __tablename__ = "session_tokens"

    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    created_at: Mapped[datetime]


class WorkbenchStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    MAINTENANCE = "maintenance"


class Workbench(Base):
    """A registered workbench. ``descriptor`` is whatever its agent last reported."""

    __tablename__ = "workbenches"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    descriptor: Mapped[dict[str, Any]]
    # Admin-controlled; "online"/"offline" is derived from the live agent connection.
    maintenance: Mapped[bool] = mapped_column(default=False)
    last_seen_at: Mapped[datetime | None]


class LeaseState(StrEnum):
    QUEUED = "queued"
    OFFERED = "offered"
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    REVOKED = "revoked"

    @property
    def terminal(self) -> bool:
        return self in (LeaseState.RELEASED, LeaseState.EXPIRED, LeaseState.REVOKED)


OPEN_LEASE_STATES = (LeaseState.QUEUED, LeaseState.OFFERED, LeaseState.ACTIVE)


class Lease(Base):
    __tablename__ = "leases"
    __table_args__ = (Index("ix_leases_workbench_state", "workbench_id", "state"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workbench_id: Mapped[str] = mapped_column(ForeignKey("workbenches.id"))
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    state: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime]
    offered_at: Mapped[datetime | None]
    offer_expires_at: Mapped[datetime | None]
    granted_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]
    end_reason: Mapped[str | None]


class File(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # None for files owned by the lab itself, such as experiment assets.
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("principals.id"))
    filename: Mapped[str]
    media_type: Mapped[str]
    size_bytes: Mapped[int]
    sha256: Mapped[str]
    created_at: Mapped[datetime]


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_principal", "principal_id", "created_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workbench_id: Mapped[str] = mapped_column(ForeignKey("workbenches.id"))
    tool: Mapped[str]
    action: Mapped[str]
    lease_id: Mapped[str] = mapped_column(ForeignKey("leases.id"))
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    experiment_run_id: Mapped[str | None] = mapped_column(ForeignKey("experiment_runs.id"))
    input: Mapped[dict[str, Any]]
    state: Mapped[str] = mapped_column(String, default=RunState.PENDING)
    output: Mapped[dict[str, Any] | None]
    error: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class ExperimentRun(Base):
    __tablename__ = "experiment_runs"
    __table_args__ = (Index("ix_experiment_runs_principal", "principal_id", "created_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    experiment_id: Mapped[str]
    experiment_version: Mapped[int]
    workbench_id: Mapped[str] = mapped_column(ForeignKey("workbenches.id"))
    lease_id: Mapped[str] = mapped_column(ForeignKey("leases.id"))
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    parameter_values: Mapped[dict[str, Any]]
    # role name -> tool name on the workbench
    bindings: Mapped[dict[str, Any]]
    state: Mapped[str] = mapped_column(String, default=RunState.PENDING)
    error: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        event.listen(self.engine.sync_engine, "connect", _sqlite_pragmas)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_schema(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    def session(self) -> AsyncSession:
        return self.sessions()

    async def close(self) -> None:
        await self.engine.dispose()


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()
