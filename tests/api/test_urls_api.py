"""Integration tests for URL drill-down listing and export routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
import csv
from datetime import UTC, datetime, timedelta
from io import StringIO
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.sqlite")
os.environ.setdefault("SECRET_KEY", "test-secret")

from seo_indexing_tracker.api.urls import router
from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.models import (
    Base,
    IndexStatus,
    IndexVerdict,
    Sitemap,
    SitemapType,
    URL,
    URLIndexStatus,
    Website,
)


@pytest.mark.asyncio
async def test_urls_api_supports_filtering_pagination_search_and_export(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'urls-api.sqlite'}"
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

    async def scoped_session() -> AsyncSession:
        return session_factory()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        website = Website(domain="filters.example", site_url="https://filters.example")
        session.add(website)
        await session.flush()
        website_id = website.id

        first_sitemap = Sitemap(
            website_id=website_id,
            url="https://filters.example/sitemap-main.xml",
            sitemap_type=SitemapType.URLSET,
        )
        second_sitemap = Sitemap(
            website_id=website_id,
            url="https://filters.example/sitemap-blog.xml",
            sitemap_type=SitemapType.URLSET,
        )
        session.add_all([first_sitemap, second_sitemap])
        await session.flush()

        now = datetime.now(UTC)
        urls = [
            URL(
                website_id=website_id,
                sitemap_id=first_sitemap.id,
                url="https://filters.example/indexed-article",
                latest_index_status=URLIndexStatus.INDEXED,
                last_checked_at=now,
            ),
            URL(
                website_id=website_id,
                sitemap_id=first_sitemap.id,
                url="https://filters.example/indexed-product",
                latest_index_status=URLIndexStatus.INDEXED,
                last_checked_at=now - timedelta(hours=1),
            ),
            URL(
                website_id=website_id,
                sitemap_id=second_sitemap.id,
                url="https://filters.example/blog-not-indexed",
                latest_index_status=URLIndexStatus.NOT_INDEXED,
                last_checked_at=now - timedelta(days=1),
            ),
            URL(
                website_id=website_id,
                sitemap_id=second_sitemap.id,
                url="https://filters.example/blog-error",
                latest_index_status=URLIndexStatus.ERROR,
                last_checked_at=now - timedelta(days=2),
            ),
            URL(
                website_id=website_id,
                url="https://filters.example/unassigned",
                latest_index_status=URLIndexStatus.UNCHECKED,
            ),
        ]
        session.add_all(urls)
        await session.flush()

        session.add_all(
            [
                IndexStatus(
                    url_id=urls[0].id,
                    coverage_state="Indexed, submitted and in sitemap",
                    verdict=IndexVerdict.PASS,
                    checked_at=now,
                    google_canonical="https://filters.example/indexed-article",
                    user_canonical="https://filters.example/indexed-article",
                    raw_response={"status": "ok"},
                ),
                IndexStatus(
                    url_id=urls[2].id,
                    coverage_state="Crawled - currently not indexed",
                    verdict=IndexVerdict.FAIL,
                    checked_at=now - timedelta(minutes=10),
                    google_canonical="https://filters.example/canonical-google",
                    user_canonical="https://filters.example/canonical-user",
                    raw_response={"status": "fail"},
                ),
            ]
        )
        await session.commit()

    app = FastAPI()
    app.include_router(router)

    async def override_get_db_session() -> AsyncIterator[AsyncSession]:
        session = await scoped_session()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    app.dependency_overrides[get_db_session] = override_get_db_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        indexed_response = await client.get(
            f"/api/websites/{website_id}/urls",
            params={"status": URLIndexStatus.INDEXED.value},
        )
        assert indexed_response.status_code == 200
        indexed_payload = indexed_response.json()
        assert indexed_payload["total_items"] == 2
        assert all(
            item["latest_index_status"] == URLIndexStatus.INDEXED.value
            for item in indexed_payload["items"]
        )

        sitemap_response = await client.get(
            f"/api/websites/{website_id}/urls",
            params={"sitemap_id": str(second_sitemap.id)},
        )
        assert sitemap_response.status_code == 200
        assert sitemap_response.json()["total_items"] == 2

        search_response = await client.get(
            f"/api/websites/{website_id}/urls",
            params={"search": "blog"},
        )
        assert search_response.status_code == 200
        search_payload = search_response.json()
        assert search_payload["total_items"] == 2
        assert all("blog" in item["url"] for item in search_payload["items"])

        page_one = await client.get(
            f"/api/websites/{website_id}/urls",
            params={"page": 1, "page_size": 2},
        )
        assert page_one.status_code == 200
        page_one_payload = page_one.json()
        assert page_one_payload["total_items"] == 5
        assert page_one_payload["total_pages"] == 3
        assert len(page_one_payload["items"]) == 2

        page_three = await client.get(
            f"/api/websites/{website_id}/urls",
            params={"page": 3, "page_size": 2},
        )
        assert page_three.status_code == 200
        page_three_payload = page_three.json()
        assert page_three_payload["page"] == 3
        assert len(page_three_payload["items"]) == 1

        export_response = await client.get(
            f"/api/websites/{website_id}/urls/export",
            params={"search": "blog"},
        )
        assert export_response.status_code == 200
        assert export_response.headers["content-type"].startswith("text/csv")
        assert "attachment;" in export_response.headers["content-disposition"]

        csv_reader = csv.DictReader(StringIO(export_response.text))
        rows = list(csv_reader)
        assert len(rows) == 2
        assert {row["latest_index_status"] for row in rows} == {
            URLIndexStatus.NOT_INDEXED.value,
            URLIndexStatus.ERROR.value,
        }
        mismatch_row = next(
            row
            for row in rows
            if row["url"] == "https://filters.example/blog-not-indexed"
        )
        assert mismatch_row["canonical_mismatch"] == "true"

    await engine.dispose()
