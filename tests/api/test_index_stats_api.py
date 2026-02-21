"""Integration tests for index stats API routes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
import os
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.sqlite")
os.environ.setdefault("SECRET_KEY", "test-secret")

from seo_indexing_tracker.api.index_stats import router
from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.models import (
    Base,
    Sitemap,
    SitemapType,
    URL,
    URLIndexStatus,
    Website,
)

SessionScopeFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass
class IndexStatsApiTestContext:
    app: FastAPI
    engine: AsyncEngine
    session_scope: SessionScopeFactory
    first_website_id: UUID
    second_website_id: UUID


async def _build_context(tmp_path: Path) -> IndexStatsApiTestContext:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'index-stats-api.sqlite'}"
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
        first_website = Website(
            domain="alpha.example", site_url="https://alpha.example"
        )
        second_website = Website(domain="beta.example", site_url="https://beta.example")
        session.add_all([first_website, second_website])
        await session.flush()

        first_sitemap = Sitemap(
            website_id=first_website.id,
            url="https://alpha.example/sitemap.xml",
            sitemap_type=SitemapType.URLSET,
        )
        second_sitemap = Sitemap(
            website_id=second_website.id,
            url="https://beta.example/sitemap.xml",
            sitemap_type=SitemapType.URLSET,
        )
        session.add_all([first_sitemap, second_sitemap])
        await session.flush()

        session.add_all(
            [
                URL(
                    website_id=first_website.id,
                    sitemap_id=first_sitemap.id,
                    url="https://alpha.example/indexed",
                    latest_index_status=URLIndexStatus.INDEXED,
                ),
                URL(
                    website_id=first_website.id,
                    sitemap_id=first_sitemap.id,
                    url="https://alpha.example/not-indexed",
                    latest_index_status=URLIndexStatus.NOT_INDEXED,
                ),
                URL(
                    website_id=first_website.id,
                    url="https://alpha.example/unassigned",
                    latest_index_status=URLIndexStatus.ERROR,
                ),
                URL(
                    website_id=second_website.id,
                    sitemap_id=second_sitemap.id,
                    url="https://beta.example/soft-404",
                    latest_index_status=URLIndexStatus.SOFT_404,
                ),
                URL(
                    website_id=second_website.id,
                    sitemap_id=second_sitemap.id,
                    url="https://beta.example/unchecked",
                    latest_index_status=URLIndexStatus.UNCHECKED,
                ),
            ]
        )

        first_website_id = first_website.id
        second_website_id = second_website.id

    app = FastAPI()
    app.include_router(router)

    async def override_get_db_session() -> AsyncIterator[AsyncSession]:
        async with scoped_session() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_get_db_session

    return IndexStatsApiTestContext(
        app=app,
        engine=engine,
        session_scope=scoped_session,
        first_website_id=first_website_id,
        second_website_id=second_website_id,
    )


@pytest.mark.asyncio
async def test_get_website_index_stats_returns_expected_aggregations(
    tmp_path: Path,
) -> None:
    context = await _build_context(tmp_path)

    try:
        transport = ASGITransport(app=context.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/api/websites/{context.first_website_id}/index-stats"
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["website_id"] == str(context.first_website_id)
        assert payload["total_urls"] == 3
        assert payload["indexed_count"] == 1
        assert payload["not_indexed_count"] == 1
        assert payload["error_count"] == 1
        assert payload["coverage_percentage"] == 33.33
        assert len(payload["per_sitemap"]) == 2
        sitemap_urls = {item["sitemap_url"] for item in payload["per_sitemap"]}
        assert sitemap_urls == {"https://alpha.example/sitemap.xml", "Unassigned"}
    finally:
        await context.engine.dispose()


@pytest.mark.asyncio
async def test_get_dashboard_index_stats_returns_cross_website_summary(
    tmp_path: Path,
) -> None:
    context = await _build_context(tmp_path)

    try:
        transport = ASGITransport(app=context.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/dashboard/index-stats")

        assert response.status_code == 200
        payload = response.json()
        assert payload["total_urls"] == 5
        assert payload["indexed_count"] == 1
        assert payload["not_indexed_count"] == 1
        assert payload["soft_404_count"] == 1
        assert payload["error_count"] == 1
        assert payload["unchecked_count"] == 1
        assert payload["coverage_percentage"] == 20.0
        assert len(payload["per_website"]) == 2
        assert payload["per_website"][0]["domain"] == "alpha.example"
        assert payload["per_website"][0]["coverage_percentage"] == 33.33
        assert payload["per_website"][1]["domain"] == "beta.example"
        assert payload["per_website"][1]["coverage_percentage"] == 0.0
    finally:
        await context.engine.dispose()
