"""Quota discovery API routes."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.models import QuotaUsage, Website
from seo_indexing_tracker.models.website import QuotaDiscoveryStatus
from seo_indexing_tracker.services.quota_discovery_service import QuotaDiscoveryService

router = APIRouter(prefix="/api", tags=["quota"])

_quota_discovery_service = QuotaDiscoveryService()


class QuotaOverrideRequest(BaseModel):
    """Request to manually override quota limits."""

    indexing_limit: int | None = None
    inspection_limit: int | None = None
    indexing_used: int | None = None
    inspection_used: int | None = None
    mode: str = "manual"  # "manual" = use overrides, "auto" = use auto-discovery


class QuotaOverrideResponse(BaseModel):
    """Response after quota override."""

    website_id: UUID
    indexing_limit: int
    indexing_used: int
    inspection_limit: int
    inspection_used: int
    mode: str
    message: str


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


@router.put(
    "/websites/{website_id}/quota",
    status_code=status.HTTP_200_OK,
    response_model=QuotaOverrideResponse,
)
async def set_quota_override(
    website_id: UUID,
    request: QuotaOverrideRequest,
    session: AsyncSession = Depends(get_db_session),
) -> QuotaOverrideResponse:
    """Manually set or override quota limits and usage."""
    website = await session.get(Website, website_id)
    if website is None:
        raise HTTPException(status_code=404, detail="Website not found")

    now = datetime.now(UTC)
    today = now.date()

    # Get or create today's usage record
    usage = await session.execute(
        select(QuotaUsage).where(
            QuotaUsage.website_id == website_id,
            QuotaUsage.date == today,
        )
    )
    usage_row = usage.scalar_one_or_none()

    if usage_row is None:
        usage_row = QuotaUsage(
            website_id=website_id,
            date=today,
            indexing_count=0,
            inspection_count=0,
        )
        session.add(usage_row)

    # Update limits if provided
    if request.indexing_limit is not None:
        website.discovered_indexing_quota = request.indexing_limit
    if request.inspection_limit is not None:
        website.discovered_inspection_quota = request.inspection_limit

    # Update used counts if provided
    if request.indexing_used is not None:
        usage_row.indexing_count = request.indexing_used
    if request.inspection_used is not None:
        usage_row.inspection_count = request.inspection_used

    # Set mode
    if request.mode == "auto":
        website.quota_discovery_status = QuotaDiscoveryStatus.DISCOVERING
    else:
        website.quota_discovery_status = QuotaDiscoveryStatus.CONFIRMED
        # Mark quota as discovered so it doesn't restart automatically
        website.quota_discovered_at = datetime.now(UTC)

    await session.commit()

    return QuotaOverrideResponse(
        website_id=website_id,
        indexing_limit=website.discovered_indexing_quota or 50,
        indexing_used=usage_row.indexing_count,
        inspection_limit=website.discovered_inspection_quota or 500,
        inspection_used=usage_row.inspection_count,
        mode=request.mode,
        message=f"Quota updated for {website.domain}",
    )
