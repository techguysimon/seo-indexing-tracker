"""Tests for priority queue management backed by URL priorities."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from seo_indexing_tracker.models import Base, URL, Website
from seo_indexing_tracker.services.priority_queue import (
    PriorityQueueService,
    calculate_url_priority,
)


@pytest.mark.asyncio
async def test_calculate_url_priority_prefers_manual_override() -> None:
    now = datetime(2026, 2, 20, 12, 0, tzinfo=UTC)

    priority = calculate_url_priority(
        lastmod=now - timedelta(days=20),
        sitemap_priority=0.1,
        manual_override=0.92,
        now=now,
    )

    assert priority == 0.92


@pytest.mark.asyncio
async def test_priority_queue_enqueue_dequeue_and_remove(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'priority-queue.sqlite'}"
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

        now = datetime.now(UTC)
        queued_urls = [
            URL(
                website_id=website.id,
                url="https://example.com/a",
                sitemap_priority=0.1,
                lastmod=now - timedelta(days=15),
            ),
            URL(
                website_id=website.id,
                url="https://example.com/b",
                sitemap_priority=0.9,
                lastmod=now - timedelta(days=1),
            ),
            URL(
                website_id=website.id,
                url="https://example.com/c",
                sitemap_priority=0.7,
                lastmod=now - timedelta(days=2),
            ),
            URL(
                website_id=website.id,
                url="https://example.com/d",
                sitemap_priority=0.2,
                lastmod=None,
                manual_priority_override=0.97,
            ),
        ]
        session.add_all(queued_urls)
        await session.flush()

        website_id = website.id
        url_ids = [url.id for url in queued_urls]

    service = PriorityQueueService(session_factory=scoped_session)

    enqueue_count = await service.enqueue_many(url_ids)
    assert enqueue_count == 4

    top_urls = await service.peek(website_id, limit=3)
    assert [url.url for url in top_urls] == [
        "https://example.com/d",
        "https://example.com/b",
        "https://example.com/c",
    ]

    dequeued = await service.dequeue(website_id, limit=2)
    assert [url.url for url in dequeued] == [
        "https://example.com/d",
        "https://example.com/b",
    ]

    async with scoped_session() as session:
        persisted_urls = {
            url.url: url
            for url in (
                await session.execute(select(URL).where(URL.website_id == website_id))
            )
            .scalars()
            .all()
        }

    assert persisted_urls["https://example.com/d"].current_priority == 0.0
    assert persisted_urls["https://example.com/b"].current_priority == 0.0
    assert persisted_urls["https://example.com/c"].current_priority > 0.0

    await service.remove(url_ids[2])

    async with scoped_session() as session:
        removed_url = await session.get(URL, url_ids[2])
        assert removed_url is not None
        assert removed_url.current_priority == 0.0
        assert removed_url.manual_priority_override is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_priority_queue_reprioritize_allows_setting_and_clearing_manual_override(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'priority-reprioritize.sqlite'}"
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

        tracked_url = URL(
            website_id=website.id,
            url="https://example.com/page",
            sitemap_priority=0.05,
            lastmod=datetime(2026, 2, 1, 0, 0, tzinfo=UTC),
        )
        session.add(tracked_url)
        await session.flush()
        url_id = tracked_url.id

    service = PriorityQueueService(session_factory=scoped_session)

    reprioritized = await service.reprioritize(url_id, manual_override=0.9)
    assert reprioritized.current_priority == 0.9
    assert reprioritized.manual_priority_override == 0.9

    without_override = await service.reprioritize(url_id, manual_override=None)
    assert without_override.manual_priority_override is None
    assert without_override.current_priority < 0.9

    await engine.dispose()
