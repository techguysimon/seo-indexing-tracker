"""Database engine and session management utilities."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool

from seo_indexing_tracker.config import Settings, get_settings
from seo_indexing_tracker.models import Base

DEFAULT_POOL_SIZE = 5
DEFAULT_MAX_OVERFLOW = 10
SQLITE_BUSY_TIMEOUT_SECONDS = 30


def _is_sqlite_url(database_url: str) -> bool:
    parsed_url = make_url(database_url)
    return parsed_url.get_backend_name() == "sqlite"


def _ensure_sqlite_database_file(database_url: str) -> None:
    parsed_url = make_url(database_url)
    if parsed_url.get_backend_name() != "sqlite":
        return

    database_path = parsed_url.database
    if database_path is None or database_path in {":memory:", ""}:
        return
    if database_path.startswith("file:"):
        return

    resolved_path = Path(database_path)
    if not resolved_path.is_absolute():
        resolved_path = Path.cwd() / resolved_path

    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.touch(exist_ok=True)


def _build_engine(database_url: str) -> AsyncEngine:
    connect_args: dict[str, int] = {}
    if _is_sqlite_url(database_url):
        connect_args["timeout"] = SQLITE_BUSY_TIMEOUT_SECONDS

    engine = create_async_engine(
        database_url,
        poolclass=AsyncAdaptedQueuePool,
        pool_pre_ping=True,
        pool_size=DEFAULT_POOL_SIZE,
        max_overflow=DEFAULT_MAX_OVERFLOW,
        connect_args=connect_args,
    )

    if _is_sqlite_url(database_url):
        _configure_sqlite_pragmas(engine)

    return engine


def _configure_sqlite_pragmas(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def apply_sqlite_pragmas(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()


settings: Settings = get_settings()
_ensure_sqlite_database_file(settings.DATABASE_URL)
engine = _build_engine(settings.DATABASE_URL)

AsyncSessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autoflush=False,
    expire_on_commit=False,
)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a transaction-scoped session with automatic commit/rollback."""

    session = AsyncSessionFactory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that provides an async database session."""

    async with session_scope() as session:
        yield session


async def initialize_database() -> None:
    """Create known tables and verify SQLite WAL mode when applicable."""

    async with engine.connect() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(text("SELECT 1"))

        if not _is_sqlite_url(str(connection.engine.url)):
            return

        wal_mode_result = await connection.execute(text("PRAGMA journal_mode;"))
        wal_mode = wal_mode_result.scalar_one()
        if str(wal_mode).lower() != "wal":
            raise RuntimeError(
                f"SQLite WAL mode was not enabled. Current mode: {wal_mode}"
            )


async def close_database() -> None:
    """Dispose database engine and release pooled connections."""

    await engine.dispose()


__all__ = [
    "AsyncSessionFactory",
    "close_database",
    "engine",
    "get_db_session",
    "initialize_database",
    "session_scope",
]
