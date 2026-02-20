"""Tests for trigger indexing error handling in web routes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
import os
from pathlib import Path
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.sqlite")
os.environ.setdefault("SECRET_KEY", "test-secret")

from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.main import create_app
from seo_indexing_tracker.models import Base, Sitemap, SitemapType, Website
from seo_indexing_tracker.services.sitemap_fetcher import (
    SitemapFetchError,
    SitemapFetchHTTPError,
)

SessionScopeFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@pytest.mark.asyncio
async def test_trigger_indexing_returns_friendly_response_for_sitemap_fetch_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'web-trigger.sqlite'}"
    engine: AsyncEngine = create_async_engine(database_url)
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
        session.add(
            Sitemap(
                website_id=website.id,
                url="https://example.com/sitemap.xml",
                sitemap_type=SitemapType.URLSET,
                is_active=True,
            )
        )
        await session.flush()
        website_id: UUID = website.id

    async def fake_discover_urls(self: object, sitemap_id: UUID) -> object:  # noqa: ARG001
        raise SitemapFetchHTTPError(
            url=(
                "https://user:password@example.com/sitemap.xml"
                "?token=super-secret#section"
            ),
            status_code=403,
        )

    monkeypatch.setattr(
        "seo_indexing_tracker.api.web.URLDiscoveryService.discover_urls",
        fake_discover_urls,
    )

    app = create_app()

    async def override_get_db_session() -> AsyncIterator[AsyncSession]:
        async with scoped_session() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_get_db_session

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            htmx_response = await client.post(
                f"/ui/websites/{website_id}/trigger",
                headers={"HX-Request": "true"},
            )
            assert htmx_response.status_code == 200
            assert "Trigger indexing failed" in htmx_response.text
            assert "hero-panel" not in htmx_response.text

            full_page_response = await client.post(f"/ui/websites/{website_id}/trigger")
            assert full_page_response.status_code == 200
            assert "Trigger indexing failed" in full_page_response.text
            assert "example.com/sitemap.xml" in full_page_response.text
            assert "user:password@" not in full_page_response.text
            assert "token=super-secret" not in full_page_response.text
            assert "#section" not in full_page_response.text
            assert "hero-panel" in full_page_response.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_trigger_indexing_handles_non_http_sitemap_fetch_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'web-trigger-non-http.sqlite'}"
    engine: AsyncEngine = create_async_engine(database_url)
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
        session.add(
            Sitemap(
                website_id=website.id,
                url="https://example.com/sitemap.xml",
                sitemap_type=SitemapType.URLSET,
                is_active=True,
            )
        )
        await session.flush()
        website_id: UUID = website.id

    async def fake_discover_urls(self: object, sitemap_id: UUID) -> object:  # noqa: ARG001
        raise SitemapFetchError("network timeout while fetching sitemap")

    monkeypatch.setattr(
        "seo_indexing_tracker.api.web.URLDiscoveryService.discover_urls",
        fake_discover_urls,
    )

    app = create_app()

    async def override_get_db_session() -> AsyncIterator[AsyncSession]:
        async with scoped_session() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_get_db_session

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            htmx_response = await client.post(
                f"/ui/websites/{website_id}/trigger",
                headers={"HX-Request": "true"},
            )
            assert htmx_response.status_code == 200
            assert "Trigger indexing failed" in htmx_response.text
            assert "hero-panel" not in htmx_response.text

            full_page_response = await client.post(f"/ui/websites/{website_id}/trigger")
            assert full_page_response.status_code == 200
            assert "Trigger indexing failed" in full_page_response.text
            assert "hero-panel" in full_page_response.text
    finally:
        await engine.dispose()
