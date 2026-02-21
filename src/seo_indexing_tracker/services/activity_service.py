"""Activity logging service for cross-cutting observability events."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.models import ActivityLog


class ActivityService:
    """Persist activity entries for APIs and dashboard widgets."""

    async def log_activity(
        self,
        *,
        session: AsyncSession,
        event_type: str,
        message: str,
        website_id: UUID | None = None,
        resource_type: str | None = None,
        resource_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ActivityLog:
        activity = ActivityLog(
            event_type=event_type,
            website_id=website_id,
            resource_type=resource_type,
            resource_id=resource_id,
            message=message,
            metadata_json=metadata,
        )
        session.add(activity)
        await session.flush()
        return activity


__all__ = ["ActivityService"]
