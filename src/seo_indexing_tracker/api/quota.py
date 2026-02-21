"""Quota discovery API routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.models import Website
from seo_indexing_tracker.services.quota_discovery_service import QuotaDiscoveryService

router = APIRouter(prefix="/api", tags=["quota"])

_quota_discovery_service = QuotaDiscoveryService()


async def _ensure_website_exists(*, session: AsyncSession, website_id: UUID) -> None:
    website = await session.scalar(select(Website.id).where(Website.id == website_id))
    if website is not None:
        return
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Website not found",
    )


@router.post(
    "/websites/{website_id}/quota/discover", status_code=status.HTTP_202_ACCEPTED
)
async def discover_quota(
    website_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    await _ensure_website_exists(session=session, website_id=website_id)
    await _quota_discovery_service.discover_quota(
        session=session, website_id=website_id
    )
    status_payload = await _quota_discovery_service.get_discovered_limits(
        session=session,
        website_id=website_id,
    )
    return {
        "website_id": str(website_id),
        "message": "Quota discovery started",
        **status_payload,
    }


@router.get("/websites/{website_id}/quota/status", status_code=status.HTTP_200_OK)
async def quota_status(
    website_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    await _ensure_website_exists(session=session, website_id=website_id)
    status_payload = await _quota_discovery_service.get_discovered_limits(
        session=session,
        website_id=website_id,
    )
    return {
        "website_id": str(website_id),
        **status_payload,
    }
