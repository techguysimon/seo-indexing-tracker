"""Tests for per-website daily quota tracking."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from seo_indexing_tracker.models import Base, QuotaDiscoveryStatus, QuotaUsage, Website
from seo_indexing_tracker.services.quota_service import QuotaAPIType
from seo_indexing_tracker.services.quota_service import (
    DailyQuotaExceededError,
    QuotaService,
    QuotaServiceSettings,
)


@pytest.mark.asyncio
async def test_quota_service_tracks_usage_and_remaining_quota(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'quota-usage.sqlite'}"
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
        website = Website(domain="example.com", site_url="https://example.com")
        session.add(website)
        await session.flush()
        website_id = website.id

    service = QuotaService(
        session_factory=scoped_session,
        settings=QuotaServiceSettings(),
    )

    remaining_after_increment = await service.increment_usage(website_id, "indexing")
    assert remaining_after_increment == 199
    assert await service.get_remaining_quota(website_id, "indexing") == 199
    assert await service.check_quota_available(website_id, "indexing", 199)
    assert not await service.check_quota_available(website_id, "indexing", 200)

    await engine.dispose()


@pytest.mark.asyncio
async def test_quota_service_uses_configured_limits_and_persists_across_instances(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'quota-custom-limits.sqlite'}"
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
        website = Website(domain="example.org", site_url="https://example.org")
        session.add(website)
        await session.flush()
        website_id = website.id

    custom_settings = QuotaServiceSettings(
        INDEXING_DAILY_QUOTA_LIMIT=2,
        INSPECTION_DAILY_QUOTA_LIMIT=3,
    )
    first_service = QuotaService(
        session_factory=scoped_session, settings=custom_settings
    )

    assert await first_service.increment_usage(website_id, "inspection") == 2
    assert await first_service.increment_usage(website_id, "inspection") == 1
    assert await first_service.increment_usage(website_id, "inspection") == 0
    with pytest.raises(DailyQuotaExceededError):
        await first_service.increment_usage(website_id, "inspection")

    second_service = QuotaService(
        session_factory=scoped_session, settings=custom_settings
    )
    assert await second_service.get_remaining_quota(website_id, "inspection") == 0

    async with scoped_session() as session:
        usage_rows = (
            (
                await session.execute(
                    select(QuotaUsage).where(QuotaUsage.website_id == website_id)
                )
            )
            .scalars()
            .all()
        )

    assert len(usage_rows) == 1
    assert usage_rows[0].inspection_count == 3

    await engine.dispose()


@pytest.mark.asyncio
async def test_quota_service_resets_when_date_changes(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'quota-date-reset.sqlite'}"
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
        website = Website(domain="example.net", site_url="https://example.net")
        session.add(website)
        await session.flush()
        website_id = website.id

    current_day = date(2026, 2, 20)

    def fake_today() -> date:
        return current_day

    service = QuotaService(
        session_factory=scoped_session,
        settings=QuotaServiceSettings(),
        today_factory=fake_today,
    )

    assert await service.increment_usage(website_id, "indexing") == 199
    assert await service.get_remaining_quota(website_id, "indexing") == 199

    current_day = date(2026, 2, 21)

    assert await service.get_remaining_quota(website_id, "indexing") == 200
    assert await service.check_quota_available(website_id, "indexing", 200)

    async with scoped_session() as session:
        usage_dates = (
            (
                await session.execute(
                    select(QuotaUsage.date).where(QuotaUsage.website_id == website_id)
                )
            )
            .scalars()
            .all()
        )

    assert usage_dates == [date(2026, 2, 20)]

    assert await service.increment_usage(website_id, "indexing") == 199

    async with scoped_session() as session:
        usage_dates = (
            (
                await session.execute(
                    select(QuotaUsage.date)
                    .where(QuotaUsage.website_id == website_id)
                    .order_by(QuotaUsage.date.asc())
                )
            )
            .scalars()
            .all()
        )

    assert usage_dates == [date(2026, 2, 20), date(2026, 2, 21)]

    await engine.dispose()


def test_quota_service_uses_configured_baseline_for_non_manual_discovery() -> None:
    website = Website(
        domain="example-baseline.com",
        site_url="https://example-baseline.com",
        discovered_indexing_quota=9_999,
        discovered_inspection_quota=8_888,
        quota_discovery_status=QuotaDiscoveryStatus.ESTIMATED,
        quota_discovery_confidence=0.99,
    )

    settings = QuotaServiceSettings(
        INDEXING_DAILY_QUOTA_LIMIT=200,
        INSPECTION_DAILY_QUOTA_LIMIT=2000,
    )

    assert (
        QuotaService._quota_limit(
            api_type=QuotaAPIType.INDEXING,
            website=website,
            settings=settings,
        )
        == 200
    )
    assert (
        QuotaService._quota_limit(
            api_type=QuotaAPIType.INSPECTION,
            website=website,
            settings=settings,
        )
        == 2000
    )


def test_quota_service_defaults_match_stable_runtime_limits() -> None:
    settings = QuotaServiceSettings()

    assert settings.INDEXING_DAILY_QUOTA_LIMIT == 200
    assert settings.INSPECTION_DAILY_QUOTA_LIMIT == 2000


def test_quota_service_discovering_values_cannot_auto_escalate_runtime_limit() -> None:
    website = Website(
        domain="example-discovering.com",
        site_url="https://example-discovering.com",
        discovered_indexing_quota=20_000,
        discovered_inspection_quota=40_000,
        quota_discovery_status=QuotaDiscoveryStatus.DISCOVERING,
        quota_discovery_confidence=0.5,
    )

    settings = QuotaServiceSettings(
        INDEXING_DAILY_QUOTA_LIMIT=200,
        INSPECTION_DAILY_QUOTA_LIMIT=2000,
    )

    assert (
        QuotaService._quota_limit(
            api_type=QuotaAPIType.INDEXING,
            website=website,
            settings=settings,
        )
        == 200
    )
    assert (
        QuotaService._quota_limit(
            api_type=QuotaAPIType.INSPECTION,
            website=website,
            settings=settings,
        )
        == 2000
    )


def test_quota_service_allows_confirmed_manual_override_limits() -> None:
    website = Website(
        domain="example-manual.com",
        site_url="https://example-manual.com",
        discovered_indexing_quota=350,
        discovered_inspection_quota=3_500,
        quota_discovery_status=QuotaDiscoveryStatus.CONFIRMED,
        quota_discovery_confidence=0.25,
    )

    settings = QuotaServiceSettings(
        INDEXING_DAILY_QUOTA_LIMIT=200,
        INSPECTION_DAILY_QUOTA_LIMIT=2000,
    )

    assert (
        QuotaService._quota_limit(
            api_type=QuotaAPIType.INDEXING,
            website=website,
            settings=settings,
        )
        == 350
    )
    assert (
        QuotaService._quota_limit(
            api_type=QuotaAPIType.INSPECTION,
            website=website,
            settings=settings,
        )
        == 3500
    )
