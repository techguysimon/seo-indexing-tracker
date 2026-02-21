"""Sitemap refresh progress API routes."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.models import Sitemap, SitemapRefreshProgress, Website

router = APIRouter(prefix="/api", tags=["sitemap-progress"])


class SitemapProgressResponse(BaseModel):
    id: UUID
    sitemap_id: UUID
    website_id: UUID
    status: str
    started_at: datetime
    updated_at: datetime
    urls_found: int
    urls_new: int
    urls_modified: int
    errors: str | None


def _to_response(progress: SitemapRefreshProgress) -> SitemapProgressResponse:
    return SitemapProgressResponse(
        id=progress.id,
        sitemap_id=progress.sitemap_id,
        website_id=progress.website_id,
        status=progress.status,
        started_at=progress.started_at,
        updated_at=progress.updated_at,
        urls_found=progress.urls_found,
        urls_new=progress.urls_new,
        urls_modified=progress.urls_modified,
        errors=progress.errors,
    )


@router.get(
    "/sitemaps/{sitemap_id}/progress",
    response_model=SitemapProgressResponse,
    status_code=status.HTTP_200_OK,
)
async def get_sitemap_progress(
    sitemap_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> SitemapProgressResponse:
    sitemap_exists = await session.scalar(
        select(Sitemap.id).where(Sitemap.id == sitemap_id)
    )
    if sitemap_exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sitemap not found",
        )

    progress = await session.scalar(
        select(SitemapRefreshProgress).where(
            SitemapRefreshProgress.sitemap_id == sitemap_id
        )
    )
    if progress is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sitemap progress not found",
        )

    return _to_response(progress)


@router.get(
    "/websites/{website_id}/sitemap-progress",
    response_model=list[SitemapProgressResponse],
    status_code=status.HTTP_200_OK,
)
async def list_website_sitemap_progress(
    website_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> list[SitemapProgressResponse]:
    website_exists = await session.scalar(
        select(Website.id).where(Website.id == website_id)
    )
    if website_exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found",
        )

    rows = (
        (
            await session.execute(
                select(SitemapRefreshProgress)
                .where(SitemapRefreshProgress.website_id == website_id)
                .order_by(SitemapRefreshProgress.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_to_response(row) for row in rows]
