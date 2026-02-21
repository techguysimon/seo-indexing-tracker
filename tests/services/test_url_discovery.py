"""Tests for sitemap URL discovery and change detection."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from seo_indexing_tracker.models import Base, Sitemap, SitemapType, URL, Website
from seo_indexing_tracker.services.sitemap_fetcher import (
    SitemapFetchNetworkError,
    SitemapFetchResult,
)
from seo_indexing_tracker.services.url_discovery import URLDiscoveryService


def _fetch_result(
    *,
    content: str | None,
    etag: str | None,
    last_modified: str | None,
    not_modified: bool,
) -> SitemapFetchResult:
    return SitemapFetchResult(
        content=content.encode("utf-8") if content is not None else None,
        etag=etag,
        last_modified=last_modified,
        status_code=304 if not_modified else 200,
        content_type="application/xml",
        url="https://example.com/sitemap.xml",
        not_modified=not_modified,
    )


@pytest.mark.asyncio
async def test_discover_urls_tracks_new_modified_and_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'discovery.sqlite'}"
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

        sitemap = Sitemap(
            website_id=website.id,
            url="https://example.com/sitemap.xml",
            sitemap_type=SitemapType.URLSET,
            etag="old-etag",
            last_modified_header="Thu, 19 Feb 2026 00:00:00 GMT",
        )
        session.add(sitemap)
        await session.flush()

        session.add_all(
            [
                URL(
                    website_id=website.id,
                    sitemap_id=sitemap.id,
                    url="https://example.com/unchanged",
                    lastmod=datetime(2026, 2, 10, 0, 0, tzinfo=UTC),
                ),
                URL(
                    website_id=website.id,
                    sitemap_id=sitemap.id,
                    url="https://example.com/modified",
                    lastmod=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
                ),
                URL(
                    website_id=website.id,
                    sitemap_id=sitemap.id,
                    url="https://example.com/no-lastmod",
                    lastmod=None,
                ),
            ]
        )

    xml_content = """
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url>
            <loc>https://example.com/unchanged</loc>
            <lastmod>2026-02-10T00:00:00Z</lastmod>
        </url>
        <url>
            <loc>https://example.com/modified</loc>
            <lastmod>2026-02-20</lastmod>
        </url>
        <url>
            <loc>https://example.com/no-lastmod</loc>
        </url>
        <url>
            <loc>https://example.com/new</loc>
            <lastmod>2026-02-18</lastmod>
        </url>
    </urlset>
    """

    async def fake_fetch_sitemap(_: str, **__: str | None) -> SitemapFetchResult:
        return _fetch_result(
            content=xml_content,
            etag="fresh-etag",
            last_modified="Fri, 20 Feb 2026 12:00:00 GMT",
            not_modified=False,
        )

    monkeypatch.setattr(
        "seo_indexing_tracker.services.url_discovery.fetch_sitemap",
        fake_fetch_sitemap,
    )

    service = URLDiscoveryService(session_factory=scoped_session)

    async with scoped_session() as session:
        sitemap_id = (
            await session.execute(
                select(Sitemap.id).where(
                    Sitemap.url == "https://example.com/sitemap.xml"
                )
            )
        ).scalar_one()

    result = await service.discover_urls(sitemap_id)

    assert result.total_discovered == 4
    assert result.new_count == 1
    assert result.modified_count == 2
    assert result.unchanged_count == 1

    async with scoped_session() as session:
        persisted_sitemap = await session.get(Sitemap, sitemap_id)
        assert persisted_sitemap is not None
        assert persisted_sitemap.last_fetched is not None
        assert persisted_sitemap.etag == "fresh-etag"
        assert persisted_sitemap.last_modified_header == "Fri, 20 Feb 2026 12:00:00 GMT"

        urls = (
            await session.execute(
                select(URL).where(URL.website_id == persisted_sitemap.website_id)
            )
        ).scalars()
        urls_by_value = {url.url: url for url in urls}

    assert len(urls_by_value) == 4
    modified_lastmod = urls_by_value["https://example.com/modified"].lastmod
    assert modified_lastmod is not None
    assert modified_lastmod.replace(tzinfo=UTC) == datetime(
        2026, 2, 20, 0, 0, tzinfo=UTC
    )
    assert urls_by_value["https://example.com/no-lastmod"].lastmod is None
    new_lastmod = urls_by_value["https://example.com/new"].lastmod
    assert new_lastmod is not None
    assert new_lastmod.replace(tzinfo=UTC) == datetime(2026, 2, 18, 0, 0, tzinfo=UTC)

    await engine.dispose()


@pytest.mark.asyncio
async def test_discover_urls_updates_sitemap_metadata_when_not_modified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'not-modified.sqlite'}"
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

        sitemap = Sitemap(
            website_id=website.id,
            url="https://example.com/sitemap.xml",
            sitemap_type=SitemapType.URLSET,
            etag="stale-etag",
            last_modified_header="Thu, 19 Feb 2026 00:00:00 GMT",
        )
        session.add(sitemap)
        await session.flush()
        sitemap_id = sitemap.id

    async def fake_fetch_sitemap(_: str, **__: str | None) -> SitemapFetchResult:
        return _fetch_result(
            content=None,
            etag="etag-304",
            last_modified="Fri, 20 Feb 2026 13:00:00 GMT",
            not_modified=True,
        )

    monkeypatch.setattr(
        "seo_indexing_tracker.services.url_discovery.fetch_sitemap",
        fake_fetch_sitemap,
    )

    service = URLDiscoveryService(session_factory=scoped_session)
    result = await service.discover_urls(sitemap_id)

    assert result.total_discovered == 0
    assert result.new_count == 0
    assert result.modified_count == 0
    assert result.unchanged_count == 0

    async with scoped_session() as session:
        persisted_sitemap = await session.get(Sitemap, sitemap_id)
        assert persisted_sitemap is not None
        assert persisted_sitemap.last_fetched is not None
        assert persisted_sitemap.etag == "etag-304"
        assert persisted_sitemap.last_modified_header == "Fri, 20 Feb 2026 13:00:00 GMT"

    await engine.dispose()


@pytest.mark.asyncio
async def test_discover_urls_expands_index_sitemap_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'index-discovery.sqlite'}"
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

        sitemap = Sitemap(
            website_id=website.id,
            url="https://example.com/sitemap-index.xml",
            sitemap_type=SitemapType.INDEX,
        )
        session.add(sitemap)
        await session.flush()
        sitemap_id = sitemap.id

    async def fake_fetch_sitemap(url: str, **__: str | None) -> SitemapFetchResult:
        payloads = {
            "https://example.com/sitemap-index.xml": """
            <sitemapindex xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">
              <sitemap><loc>https://example.com/news-sitemap.xml</loc></sitemap>
              <sitemap><loc>https://example.com/pages-sitemap.xml</loc></sitemap>
            </sitemapindex>
            """,
            "https://example.com/news-sitemap.xml": """
            <urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">
              <url><loc>https://example.com/news/a</loc></url>
              <url><loc>https://example.com/news/b</loc></url>
            </urlset>
            """,
            "https://example.com/pages-sitemap.xml": """
            <urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">
              <url><loc>https://example.com/about</loc></url>
            </urlset>
            """,
        }
        return _fetch_result(
            content=payloads[url],
            etag=None,
            last_modified=None,
            not_modified=False,
        )

    monkeypatch.setattr(
        "seo_indexing_tracker.services.url_discovery.fetch_sitemap",
        fake_fetch_sitemap,
    )

    result = await URLDiscoveryService(session_factory=scoped_session).discover_urls(
        sitemap_id
    )

    assert result.total_discovered == 3
    assert result.new_count == 3
    assert result.modified_count == 0
    assert result.unchanged_count == 0

    async with scoped_session() as session:
        persisted_urls = (
            (await session.execute(select(URL.url).order_by(URL.url.asc())))
            .scalars()
            .all()
        )

    assert persisted_urls == [
        "https://example.com/about",
        "https://example.com/news/a",
        "https://example.com/news/b",
    ]

    await engine.dispose()


@pytest.mark.asyncio
async def test_discover_urls_deduplicates_duplicate_child_sitemaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'index-dedup.sqlite'}"
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

        sitemap = Sitemap(
            website_id=website.id,
            url="https://example.com/sitemap-index.xml",
            sitemap_type=SitemapType.INDEX,
        )
        session.add(sitemap)
        await session.flush()
        sitemap_id = sitemap.id

    fetch_calls: list[str] = []

    async def fake_fetch_sitemap(url: str, **__: str | None) -> SitemapFetchResult:
        fetch_calls.append(url)
        payloads = {
            "https://example.com/sitemap-index.xml": """
            <sitemapindex xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">
              <sitemap><loc>https://example.com/shared-sitemap.xml</loc></sitemap>
              <sitemap><loc>https://example.com/shared-sitemap.xml</loc></sitemap>
            </sitemapindex>
            """,
            "https://example.com/shared-sitemap.xml": """
            <urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">
              <url><loc>https://example.com/only-once</loc></url>
            </urlset>
            """,
        }
        return _fetch_result(
            content=payloads[url],
            etag=None,
            last_modified=None,
            not_modified=False,
        )

    monkeypatch.setattr(
        "seo_indexing_tracker.services.url_discovery.fetch_sitemap",
        fake_fetch_sitemap,
    )

    result = await URLDiscoveryService(session_factory=scoped_session).discover_urls(
        sitemap_id
    )

    assert result.total_discovered == 1
    assert fetch_calls.count("https://example.com/shared-sitemap.xml") == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_discover_urls_fails_fast_when_child_sitemap_fetch_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'index-fetch-failure.sqlite'}"
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

        sitemap = Sitemap(
            website_id=website.id,
            url="https://example.com/sitemap-index.xml",
            sitemap_type=SitemapType.INDEX,
        )
        session.add(sitemap)
        await session.flush()
        sitemap_id = sitemap.id

    async def fake_fetch_sitemap(url: str, **__: str | None) -> SitemapFetchResult:
        if url == "https://example.com/sitemap-index.xml":
            return _fetch_result(
                content="""
                <sitemapindex xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">
                  <sitemap><loc>https://example.com/failing-child.xml</loc></sitemap>
                  <sitemap><loc>https://example.com/success-child.xml</loc></sitemap>
                </sitemapindex>
                """,
                etag=None,
                last_modified=None,
                not_modified=False,
            )

        if url == "https://example.com/failing-child.xml":
            raise SitemapFetchNetworkError("child sitemap network failure")

        return _fetch_result(
            content="""
            <urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">
              <url><loc>https://example.com/should-not-be-processed</loc></url>
            </urlset>
            """,
            etag=None,
            last_modified=None,
            not_modified=False,
        )

    monkeypatch.setattr(
        "seo_indexing_tracker.services.url_discovery.fetch_sitemap",
        fake_fetch_sitemap,
    )

    with pytest.raises(SitemapFetchNetworkError):
        await URLDiscoveryService(session_factory=scoped_session).discover_urls(
            sitemap_id
        )

    async with scoped_session() as session:
        persisted_url_count = int(
            (
                await session.scalar(
                    select(func.count())
                    .select_from(URL)
                    .where(URL.website_id == website.id)
                )
            )
            or 0
        )

    assert persisted_url_count == 0

    await engine.dispose()
