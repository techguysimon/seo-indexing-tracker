"""Activity log ORM model for recent system events."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from seo_indexing_tracker.models.base import Base


class ActivityLog(Base):
    """Store structured activity events for dashboard and API consumption."""

    __tablename__ = "activity_logs"
    __table_args__ = (
        Index("ix_activity_logs_created_at", "created_at"),
        Index("ix_activity_logs_event_type", "event_type"),
        Index("ix_activity_logs_website_created_at", "website_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    website_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("websites.id", ondelete="SET NULL"),
    )
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    message: Mapped[str] = mapped_column(String(512), nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


__all__ = ["ActivityLog"]
