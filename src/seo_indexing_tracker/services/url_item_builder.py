"""Shared URL item builder for constructing URL dicts matching WebsiteURLListItem structure."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.models import IndexStatus, URL


async def build_url_item(
    session: AsyncSession,
    url_id: UUID,
) -> dict[str, Any]:
    """Build a URL item dict matching WebsiteURLListItem structure."""
    latest_status_subquery = (
        select(
            IndexStatus.url_id.label("url_id"),
            func.max(IndexStatus.checked_at).label("checked_at"),
        )
        .where(IndexStatus.url_id == url_id)
        .group_by(IndexStatus.url_id)
        .subquery()
    )

    row = await session.execute(
        select(
            URL.url,
            URL.latest_index_status,
            URL.last_checked_at,
            URL.last_submitted_at,
            URL.sitemap_id,
            IndexStatus.verdict,
            IndexStatus.coverage_state,
            IndexStatus.last_crawl_time,
            IndexStatus.google_canonical,
            IndexStatus.user_canonical,
        )
        .where(URL.id == url_id)
        .outerjoin(latest_status_subquery, latest_status_subquery.c.url_id == URL.id)
        .outerjoin(
            IndexStatus,
            (IndexStatus.url_id == latest_status_subquery.c.url_id)
            & (IndexStatus.checked_at == latest_status_subquery.c.checked_at),
        )
    )
    result = row.first()
    if result is None:
        return {
            "id": url_id,
            "url": "",
            "latest_index_status": "UNCHECKED",
            "last_checked_at": None,
            "last_submitted_at": None,
            "sitemap_id": None,
            "verdict": None,
            "coverage_state": None,
            "last_crawl_time": None,
            "google_canonical": None,
            "user_canonical": None,
        }

    return {
        "id": url_id,
        "url": result.url,
        "latest_index_status": result.latest_index_status,
        "last_checked_at": result.last_checked_at,
        "last_submitted_at": result.last_submitted_at,
        "sitemap_id": result.sitemap_id,
        "verdict": result.verdict,
        "coverage_state": result.coverage_state,
        "last_crawl_time": result.last_crawl_time,
        "google_canonical": result.google_canonical,
        "user_canonical": result.user_canonical,
    }
