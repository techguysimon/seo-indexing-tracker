"""Tests for trigger indexing error handling in web routes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from datetime import UTC, datetime
import logging
import os
from pathlib import Path
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
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
from seo_indexing_tracker.models import Base, Sitemap, SitemapType, URL, Website
from seo_indexing_tracker.services.sitemap_fetcher import (
    SitemapFetchHTTPError,
    SitemapFetchNetworkError,
    SitemapFetchResult,
    SitemapFetchTimeoutError,
)
from seo_indexing_tracker.services.url_discovery import (
    URLDiscoveryProcessingError,
    URLDiscoveryResult,
)

SessionScopeFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
SENSITIVE_SITEMAP_URL = (
    "https://user:password@example.com/sitemap.xml?token=super-secret#section"
)
SANITIZED_SITEMAP_URL = "example.com/sitemap.xml"


def _trigger_log_payloads(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for record in caplog.records:
        if isinstance(record.msg, dict):
            payloads.append(record.msg)
            continue

        if isinstance(record.args, dict):
            payloads.append(record.args)
            continue

        if (
            isinstance(record.args, tuple)
            and len(record.args) == 1
            and isinstance(record.args[0], dict)
        ):
            payloads.append(record.args[0])

    return payloads


def _assert_payloads_do_not_leak_secrets(payloads: list[dict[str, object]]) -> None:
    for payload in payloads:
        for value in payload.values():
            if not isinstance(value, str):
                continue
            assert "user:password@" not in value
            assert "token=super-secret" not in value
            assert "#section" not in value


@contextmanager
def capture_trigger_indexing_logs(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    trigger_logger = logging.getLogger("seo_indexing_tracker.web.trigger_indexing")
    original_propagate = trigger_logger.propagate
    trigger_logger.addHandler(caplog.handler)
    trigger_logger.propagate = True
    caplog.set_level(logging.ERROR, logger="seo_indexing_tracker.web.trigger_indexing")
    try:
        yield
    finally:
        trigger_logger.removeHandler(caplog.handler)
        trigger_logger.propagate = original_propagate


@pytest.mark.asyncio
async def test_trigger_indexing_returns_parse_message_for_non_xml_sitemap_response(
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

    async def fake_fetch_sitemap(_: str, **__: str | None) -> SitemapFetchResult:
        return SitemapFetchResult(
            content=b"this is not xml",
            etag=None,
            last_modified=None,
            status_code=200,
            content_type="text/html",
            url=(
                "https://user:password@example.com/sitemap.xml"
                "?token=super-secret#section"
            ),
            not_modified=False,
        )

    monkeypatch.setattr(
        "seo_indexing_tracker.services.url_discovery.fetch_sitemap",
        fake_fetch_sitemap,
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
            assert "not valid XML" in htmx_response.text
            assert "hero-panel" not in htmx_response.text

            full_page_response = await client.post(f"/ui/websites/{website_id}/trigger")
            assert full_page_response.status_code == 200
            assert "Trigger indexing failed" in full_page_response.text
            assert "not valid XML" in full_page_response.text
            assert "example.com/sitemap.xml" in full_page_response.text
            assert "user:password@" not in full_page_response.text
            assert "token=super-secret" not in full_page_response.text
            assert "#section" not in full_page_response.text
            assert "hero-panel" in full_page_response.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_text"),
    [
        (
            SitemapFetchTimeoutError("timeout after retries"),
            "network timeout while fetching sitemap",
        ),
        (
            SitemapFetchNetworkError("network unreachable"),
            "network error while fetching sitemap",
        ),
    ],
)
async def test_trigger_indexing_handles_timeout_and_network_sitemap_fetch_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_text: str,
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
        raise error

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
            assert expected_text in htmx_response.text
            assert "hero-panel" not in htmx_response.text

            full_page_response = await client.post(f"/ui/websites/{website_id}/trigger")
            assert full_page_response.status_code == 200
            assert "Trigger indexing failed" in full_page_response.text
            assert expected_text in full_page_response.text
            assert "hero-panel" in full_page_response.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_text"),
    [
        (401, "sitemap access blocked"),
        (403, "sitemap access blocked"),
        (500, "sitemap fetch returned an HTTP error"),
    ],
)
async def test_trigger_indexing_handles_sitemap_fetch_http_status_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status_code: int,
    expected_text: str,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'web-trigger-http-status.sqlite'}"
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
                "?token=super-secret#fragment"
            ),
            status_code=status_code,
            content_type="text/html",
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
        with capture_trigger_indexing_logs(caplog):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.post(f"/ui/websites/{website_id}/trigger")
                assert response.status_code == 200
                assert "Trigger indexing failed" in response.text
                assert expected_text in response.text
                assert "example.com/sitemap.xml" in response.text
                assert "user:password@" not in response.text
                assert "token=super-secret" not in response.text
                assert "#fragment" not in response.text

            payloads = _trigger_log_payloads(caplog)
            assert any(
                payload.get("stage") == "fetch"
                and payload.get("sitemap_url_sanitized") == SANITIZED_SITEMAP_URL
                for payload in payloads
            )
            _assert_payloads_do_not_leak_secrets(payloads)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_trigger_indexing_handles_discovery_stage_failures_with_friendly_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database_url = (
        f"sqlite+aiosqlite:///{tmp_path / 'web-trigger-discovery-stage.sqlite'}"
    )
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
        sitemap = Sitemap(
            website_id=website.id,
            url=SENSITIVE_SITEMAP_URL,
            sitemap_type=SitemapType.URLSET,
            is_active=True,
        )
        session.add(sitemap)
        await session.flush()
        website_id: UUID = website.id

    async def fake_discover_urls(self: object, discovered_sitemap_id: UUID) -> object:  # noqa: ARG001
        raise URLDiscoveryProcessingError(
            stage="discovery",
            website_id=website_id,
            sitemap_id=discovered_sitemap_id,
            sitemap_url=SENSITIVE_SITEMAP_URL,
            status_code=200,
            content_type="application/xml",
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
        with capture_trigger_indexing_logs(caplog):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.post(f"/ui/websites/{website_id}/trigger")
                assert response.status_code == 200
                assert "Trigger indexing failed" in response.text
                assert "sitemap discovery failed" in response.text
                assert "Internal Server Error" not in response.text

            payloads = _trigger_log_payloads(caplog)
            assert any(
                payload.get("stage") == "discovery"
                and payload.get("sitemap_url_sanitized") == SANITIZED_SITEMAP_URL
                for payload in payloads
            )
            _assert_payloads_do_not_leak_secrets(payloads)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_trigger_indexing_enqueue_failure_rolls_back_discovered_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database_url = (
        f"sqlite+aiosqlite:///{tmp_path / 'web-trigger-enqueue-rollback.sqlite'}"
    )
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
        sitemap = Sitemap(
            website_id=website.id,
            url=SENSITIVE_SITEMAP_URL,
            sitemap_type=SitemapType.URLSET,
            is_active=True,
        )
        session.add(sitemap)
        await session.flush()
        website_id: UUID = website.id
        sitemap_id: UUID = sitemap.id

    async def fake_discover_urls(
        self: object,
        discovered_sitemap_id: UUID,
    ) -> URLDiscoveryResult:
        async with self._session_factory() as session:  # type: ignore[attr-defined]
            sitemap = await session.get(Sitemap, discovered_sitemap_id)
            assert sitemap is not None
            sitemap.etag = "temporary-etag"
            sitemap.last_modified_header = "Wed, 21 Oct 2015 07:28:00 GMT"
            sitemap.last_fetched = datetime.now(UTC)
            session.add(
                URL(
                    website_id=sitemap.website_id,
                    sitemap_id=sitemap.id,
                    url="https://example.com/new-page",
                )
            )
            await session.flush()

        return URLDiscoveryResult(
            total_discovered=1,
            new_count=1,
            modified_count=0,
            unchanged_count=0,
        )

    async def fake_enqueue_many(self: object, url_ids: list[UUID]) -> int:  # noqa: ARG001
        raise RuntimeError("forced enqueue failure")

    monkeypatch.setattr(
        "seo_indexing_tracker.api.web.URLDiscoveryService.discover_urls",
        fake_discover_urls,
    )
    monkeypatch.setattr(
        "seo_indexing_tracker.api.web.PriorityQueueService.enqueue_many",
        fake_enqueue_many,
    )

    app = create_app()

    async def override_get_db_session() -> AsyncIterator[AsyncSession]:
        async with scoped_session() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_get_db_session

    try:
        with capture_trigger_indexing_logs(caplog):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.post(f"/ui/websites/{website_id}/trigger")
                assert response.status_code == 200
                assert "Trigger indexing failed" in response.text
                assert "could not be queued" in response.text

            payloads = _trigger_log_payloads(caplog)
            assert any(
                payload.get("stage") == "enqueue"
                and payload.get("sitemap_url_sanitized") == SANITIZED_SITEMAP_URL
                for payload in payloads
            )
            _assert_payloads_do_not_leak_secrets(payloads)

        async with scoped_session() as session:
            persisted_urls = await session.scalars(
                select(URL).where(URL.website_id == website_id)
            )
            assert list(persisted_urls) == []

            persisted_sitemap = await session.get(Sitemap, sitemap_id)
            assert persisted_sitemap is not None
            assert persisted_sitemap.etag is None
            assert persisted_sitemap.last_modified_header is None
            assert persisted_sitemap.last_fetched is None
    finally:
        await engine.dispose()
