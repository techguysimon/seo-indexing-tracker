"""Tests for adaptive quota discovery behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from seo_indexing_tracker.models import (
    ActivityLog,
    Base,
    QuotaDiscoveryStatus,
    QuotaUsage,
    Website,
)
from seo_indexing_tracker.services.quota_discovery_service import QuotaDiscoveryService


@pytest.mark.asyncio
async def test_discover_quota_sets_initial_state_and_logs_activity(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'quota-discovery-initial.sqlite'}"
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

    @asynccontextmanager
    async def scoped_session() -> AsyncIterator[AsyncSession]:
        session = session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with scoped_session() as session:
        website = Website(domain="quota.example", site_url="https://quota.example")
        session.add(website)
        await session.flush()
        website_id = website.id

    service = QuotaDiscoveryService()
    async with scoped_session() as session:
        await service.discover_quota(session=session, website_id=website_id)

    async with scoped_session() as session:
        refreshed = await session.get(Website, website_id)
        assert refreshed is not None
        assert refreshed.discovered_indexing_quota == 50
        assert refreshed.discovered_inspection_quota == 500
        assert refreshed.quota_discovery_status == QuotaDiscoveryStatus.DISCOVERING
        assert refreshed.quota_discovery_confidence == pytest.approx(0.1)
        assert refreshed.quota_discovered_at is not None

        log_rows = (
            (
                await session.execute(
                    select(ActivityLog).where(ActivityLog.website_id == website_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(log_rows) == 1
        assert log_rows[0].event_type == "quota_discovered"

    await engine.dispose()


@pytest.mark.asyncio
async def test_record_429_reduces_quota_and_confidence(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'quota-discovery-429.sqlite'}"
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

    @asynccontextmanager
    async def scoped_session() -> AsyncIterator[AsyncSession]:
        session = session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with scoped_session() as session:
        website = Website(
            domain="pressure.example",
            site_url="https://pressure.example",
            discovered_indexing_quota=100,
            quota_discovery_status=QuotaDiscoveryStatus.DISCOVERING,
            quota_discovery_confidence=0.5,
        )
        session.add(website)
        await session.flush()
        website_id = website.id

    service = QuotaDiscoveryService()
    async with scoped_session() as session:
        await service.record_429(
            session=session,
            website_id=website_id,
            api_type="indexing",
        )

    async with scoped_session() as session:
        refreshed = await session.get(Website, website_id)
        assert refreshed is not None
        assert refreshed.discovered_indexing_quota == 90
        assert refreshed.quota_discovery_confidence == pytest.approx(0.25)
        assert refreshed.quota_discovery_status == QuotaDiscoveryStatus.ESTIMATED
        assert refreshed.quota_last_429_at is not None

    await engine.dispose()


@pytest.mark.asyncio
async def test_record_success_increases_confidence_and_transitions_state(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'quota-discovery-success.sqlite'}"
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

    @asynccontextmanager
    async def scoped_session() -> AsyncIterator[AsyncSession]:
        session = session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    today = datetime.now(UTC).date()
    async with scoped_session() as session:
        website = Website(
            domain="confidence.example",
            site_url="https://confidence.example",
            discovered_indexing_quota=50,
            quota_discovery_status=QuotaDiscoveryStatus.PENDING,
            quota_discovery_confidence=0.0,
        )
        session.add(website)
        await session.flush()
        website_id = website.id

        session.add(
            QuotaUsage(
                website_id=website_id,
                date=today,
                indexing_count=10,
                inspection_count=0,
            )
        )

    service = QuotaDiscoveryService()
    async with scoped_session() as session:
        await service.record_success(
            session=session,
            website_id=website_id,
            api_type="indexing",
        )

    async with scoped_session() as session:
        refreshed = await session.get(Website, website_id)
        assert refreshed is not None
        assert refreshed.quota_discovery_status == QuotaDiscoveryStatus.DISCOVERING
        assert refreshed.quota_discovery_confidence == pytest.approx(0.1)

        refreshed.quota_discovery_status = QuotaDiscoveryStatus.DISCOVERING
        refreshed.quota_discovery_confidence = 0.2
        refreshed.discovered_indexing_quota = 50

    async with scoped_session() as session:
        await service.record_success(
            session=session,
            website_id=website_id,
            api_type="indexing",
        )

    async with scoped_session() as session:
        refreshed = await session.get(Website, website_id)
        assert refreshed is not None
        assert refreshed.discovered_indexing_quota == 56
        assert refreshed.quota_discovery_status == QuotaDiscoveryStatus.ESTIMATED
        assert refreshed.quota_discovery_confidence == pytest.approx(0.21)

        usage = await session.scalar(
            select(QuotaUsage).where(
                QuotaUsage.website_id == website_id,
                QuotaUsage.date == today,
            )
        )
        assert usage is not None
        usage.indexing_count = 50
        refreshed.quota_discovery_confidence = 0.95

    async with scoped_session() as session:
        await service.record_success(
            session=session,
            website_id=website_id,
            api_type="indexing",
        )

    async with scoped_session() as session:
        refreshed = await session.get(Website, website_id)
        assert refreshed is not None
        assert refreshed.quota_discovery_status == QuotaDiscoveryStatus.CONFIRMED
        assert refreshed.quota_discovery_confidence == pytest.approx(0.96)

    await engine.dispose()


@pytest.mark.asyncio
async def test_record_429_with_retry_after_uses_smaller_penalty_and_floor(
    tmp_path: Path,
) -> None:
    database_url = (
        f"sqlite+aiosqlite:///{tmp_path / 'quota-discovery-retry-after.sqlite'}"
    )
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

    @asynccontextmanager
    async def scoped_session() -> AsyncIterator[AsyncSession]:
        session = session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with scoped_session() as session:
        website = Website(
            domain="retry-after.example",
            site_url="https://retry-after.example",
            discovered_indexing_quota=52,
            quota_discovery_status=QuotaDiscoveryStatus.DISCOVERING,
            quota_discovery_confidence=0.2,
        )
        session.add(website)
        await session.flush()
        website_id = website.id

    service = QuotaDiscoveryService()
    async with scoped_session() as session:
        await service.record_429(
            session=session,
            website_id=website_id,
            api_type="indexing",
            retry_after_seconds=120,
        )

    async with scoped_session() as session:
        refreshed = await session.get(Website, website_id)
        assert refreshed is not None
        assert refreshed.discovered_indexing_quota == 50
        assert refreshed.quota_discovery_confidence == pytest.approx(0.05)
        assert refreshed.quota_discovery_status == QuotaDiscoveryStatus.FAILED

    await engine.dispose()
