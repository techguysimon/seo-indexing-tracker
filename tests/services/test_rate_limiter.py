"""Tests for per-website token bucket and concurrency limiting."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from seo_indexing_tracker.models import Base, RateLimitState, Website
from seo_indexing_tracker.services.quota_service import (
    QuotaService,
    QuotaServiceSettings,
)
from seo_indexing_tracker.services.rate_limiter import (
    ConcurrentRequestLimitExceededError,
    RateLimiterService,
    RateLimitTokenUnavailableError,
)


@pytest.mark.asyncio
async def test_rate_limiter_token_bucket_is_per_website(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'rate-limiter-per-website.sqlite'}"
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
        primary = Website(
            domain="example.com",
            site_url="https://example.com",
            rate_limit_bucket_size=1,
            rate_limit_refill_rate=0.01,
            rate_limit_max_concurrent_requests=2,
        )
        secondary = Website(
            domain="example.org",
            site_url="https://example.org",
            rate_limit_bucket_size=2,
            rate_limit_refill_rate=0.01,
            rate_limit_max_concurrent_requests=2,
        )
        session.add_all([primary, secondary])
        await session.flush()
        primary_id = primary.id
        secondary_id = secondary.id

    quota_service = QuotaService(
        session_factory=scoped_session,
        settings=QuotaServiceSettings(),
    )
    limiter = RateLimiterService(
        session_factory=scoped_session,
        quota_service=quota_service,
    )

    first_permit = await limiter.acquire(
        primary_id,
        api_type="indexing",
        block_until_token_available=False,
    )
    first_permit.release()

    with pytest.raises(RateLimitTokenUnavailableError):
        await limiter.acquire(
            primary_id,
            api_type="indexing",
            block_until_token_available=False,
        )

    secondary_first = await limiter.acquire(
        secondary_id,
        api_type="indexing",
        block_until_token_available=False,
    )
    secondary_first.release()
    secondary_second = await limiter.acquire(
        secondary_id,
        api_type="indexing",
        block_until_token_available=False,
    )
    secondary_second.release()

    await engine.dispose()


@pytest.mark.asyncio
async def test_rate_limiter_persists_state_across_instances(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'rate-limiter-persistence.sqlite'}"
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
            domain="example.net",
            site_url="https://example.net",
            rate_limit_bucket_size=1,
            rate_limit_refill_rate=0.01,
            rate_limit_max_concurrent_requests=2,
        )
        session.add(website)
        await session.flush()
        website_id = website.id

    quota_service = QuotaService(
        session_factory=scoped_session,
        settings=QuotaServiceSettings(),
    )

    first_limiter = RateLimiterService(
        session_factory=scoped_session,
        quota_service=quota_service,
    )
    first_permit = await first_limiter.acquire(
        website_id,
        api_type="indexing",
        block_until_token_available=False,
    )
    first_permit.release()

    async with scoped_session() as session:
        persisted_state = await session.scalar(
            select(RateLimitState).where(RateLimitState.website_id == website_id)
        )

    assert persisted_state is not None
    assert persisted_state.token_count == 0

    second_limiter = RateLimiterService(
        session_factory=scoped_session,
        quota_service=quota_service,
    )

    with pytest.raises(RateLimitTokenUnavailableError):
        await second_limiter.acquire(
            website_id,
            api_type="indexing",
            block_until_token_available=False,
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_rate_limiter_enforces_concurrency_with_queue_or_error(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'rate-limiter-concurrency.sqlite'}"
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
            domain="example.edu",
            site_url="https://example.edu",
            rate_limit_bucket_size=20,
            rate_limit_refill_rate=20,
            rate_limit_max_concurrent_requests=1,
            rate_limit_queue_excess_requests=True,
        )
        session.add(website)
        await session.flush()
        website_id = website.id

    quota_service = QuotaService(
        session_factory=scoped_session,
        settings=QuotaServiceSettings(),
    )
    limiter = RateLimiterService(
        session_factory=scoped_session,
        quota_service=quota_service,
    )

    held = await limiter.acquire(website_id, api_type="indexing")
    queued_task = asyncio.create_task(
        limiter.acquire(
            website_id,
            api_type="indexing",
            timeout_seconds=0.5,
        )
    )
    await asyncio.sleep(0.05)
    held.release()
    queued_permit = await queued_task
    queued_permit.release()

    held_again = await limiter.acquire(website_id, api_type="indexing")
    with pytest.raises(ConcurrentRequestLimitExceededError):
        await limiter.acquire(
            website_id,
            api_type="indexing",
            queue_excess_requests=False,
        )
    held_again.release()

    await engine.dispose()
