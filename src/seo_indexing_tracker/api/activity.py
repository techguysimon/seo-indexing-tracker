"""Activity log API routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.models import ActivityLog

router = APIRouter(prefix="/api", tags=["activity"])


class ActivityItemResponse(BaseModel):
    id: UUID
    event_type: str
    website_id: UUID | None
    resource_type: str | None
    resource_id: UUID | None
    message: str
    metadata: dict[str, Any] | None
    created_at: datetime


class ActivityPageResponse(BaseModel):
    page: int
    page_size: int
    total_items: int
    total_pages: int
    items: list[ActivityItemResponse] = Field(default_factory=list)


@router.get("/activity", response_model=ActivityPageResponse)
async def list_activity(
    page: int = 1,
    page_size: int = 20,
    event_type: str | None = None,
    website_id: UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> ActivityPageResponse:
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)

    statement = select(ActivityLog)
    if event_type:
        statement = statement.where(ActivityLog.event_type == event_type.strip())
    if website_id is not None:
        statement = statement.where(ActivityLog.website_id == website_id)
    if date_from is not None:
        statement = statement.where(ActivityLog.created_at >= date_from)
    if date_to is not None:
        statement = statement.where(ActivityLog.created_at <= date_to)

    total_items = int(
        (await session.scalar(select(func.count()).select_from(statement.subquery())))
        or 0
    )
    total_pages = max(1, ((total_items - 1) // safe_page_size) + 1)
    bounded_page = min(safe_page, total_pages)

    rows = (
        (
            await session.execute(
                statement.order_by(ActivityLog.created_at.desc())
                .offset((bounded_page - 1) * safe_page_size)
                .limit(safe_page_size)
            )
        )
        .scalars()
        .all()
    )

    return ActivityPageResponse(
        page=bounded_page,
        page_size=safe_page_size,
        total_items=total_items,
        total_pages=total_pages,
        items=[
            ActivityItemResponse(
                id=row.id,
                event_type=row.event_type,
                website_id=row.website_id,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                message=row.message,
                metadata=row.metadata_json,
                created_at=row.created_at,
            )
            for row in rows
        ],
    )
