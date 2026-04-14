"""Website detail rendering service for the web UI layer."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from seo_indexing_tracker.models import SitemapRefreshProgress, Website
from seo_indexing_tracker.models import SitemapType
from seo_indexing_tracker.services.index_stats_service import IndexStatsService
from seo_indexing_tracker.services.queue_eta_service import QueueETAService
from seo_indexing_tracker.services.quota_discovery_service import QuotaDiscoveryService


async def fetch_website_with_details(
    session: AsyncSession,
    website_id: UUID,
) -> Website | None:
    """Fetch a website with its service account and sitemaps eagerly loaded."""
    statement = (
        select(Website)
        .where(Website.id == website_id)
        .options(
            selectinload(Website.service_account),
            selectinload(Website.sitemaps),
        )
    )
    website: Website | None = await session.scalar(statement)
    return website


async def fetch_sitemap_progress_by_sitemap_ids(
    session: AsyncSession,
    sitemap_ids: list[UUID],
) -> dict[UUID, SitemapRefreshProgress]:
    """Fetch progress records indexed by sitemap ID."""
    if not sitemap_ids:
        return {}

    rows = (
        (
            await session.execute(
                select(SitemapRefreshProgress).where(
                    SitemapRefreshProgress.sitemap_id.in_(sitemap_ids)
                )
            )
        )
        .scalars()
        .all()
    )

    return {row.sitemap_id: row for row in rows}


async def get_website_eta_context(
    session: AsyncSession,
    website_id: UUID,
) -> dict[str, Any] | None:
    """Get ETA data for a specific website."""
    eta_service = QueueETAService(session)
    eta = await eta_service.get_website_eta(website_id)
    if eta is None:
        return None
    return {
        "website_id": str(eta.website_id),
        "website_domain": eta.website_domain,
        "status": eta.status,
        "submission_queue": {
            "queued": eta.submission_queue.queued,
            "quota_remaining": eta.submission_queue.quota_remaining,
            "quota_limit": eta.submission_queue.quota_limit,
            "eta_minutes": eta.submission_queue.eta_minutes,
            "rate_per_minute": round(eta.submission_queue.rate_per_minute, 1),
        },
        "verification_queue": {
            "queued": eta.verification_queue.queued,
            "quota_remaining": eta.verification_queue.quota_remaining,
            "quota_limit": eta.verification_queue.quota_limit,
            "eta_minutes": eta.verification_queue.eta_minutes,
            "rate_per_minute": round(eta.verification_queue.rate_per_minute, 1),
        },
        "quota_reset_at": eta.quota_reset_at.isoformat(),
    }


async def build_website_detail_context(
    session: AsyncSession,
    website_id: UUID,
    feedback: str | None = None,
) -> dict[str, Any]:
    """Build the context dict for website detail rendering.

    Raises HTTPException if website is not found.
    """
    website = await fetch_website_with_details(session=session, website_id=website_id)
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found",
        )

    sitemaps = sorted(
        website.sitemaps,
        key=lambda sitemap: sitemap.created_at,
        reverse=True,
    )
    sitemap_ids = [s.id for s in sitemaps]
    sitemap_progress = await fetch_sitemap_progress_by_sitemap_ids(
        session=session,
        sitemap_ids=sitemap_ids,
    )

    quota_discovery_service = QuotaDiscoveryService()

    return {
        "page_title": f"{website.domain} Setup",
        "website": website,
        "service_account": website.service_account,
        "sitemaps": sitemaps,
        "sitemap_progress": sitemap_progress,
        "sitemap_types": [SitemapType.URLSET.value, SitemapType.INDEX.value],
        "feedback": feedback,
        "index_stats": await IndexStatsService.get_website_index_stats(
            session=session,
            website_id=website_id,
        ),
        "quota_status": await quota_discovery_service.get_discovered_limits(
            session=session,
            website_id=website_id,
        ),
        "website_eta": await get_website_eta_context(session, website_id),
    }
