"""Index coverage statistics API routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.models import Website
from seo_indexing_tracker.services.index_stats_service import IndexStatsService

router = APIRouter(prefix="/api", tags=["index-stats"])


async def _ensure_website_exists(*, session: AsyncSession, website_id: UUID) -> None:
    website = await session.scalar(select(Website.id).where(Website.id == website_id))
    if website is not None:
        return

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Website not found",
    )


@router.get("/websites/{website_id}/index-stats", status_code=status.HTTP_200_OK)
async def get_website_index_stats(
    website_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    await _ensure_website_exists(session=session, website_id=website_id)
    return await IndexStatsService.get_website_index_stats(
        session=session,
        website_id=website_id,
    )


@router.get("/dashboard/index-stats", status_code=status.HTTP_200_OK)
async def get_dashboard_index_stats(
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    return await IndexStatsService.get_dashboard_index_stats(session=session)
