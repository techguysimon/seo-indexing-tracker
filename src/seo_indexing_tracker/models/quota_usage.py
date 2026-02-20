"""Daily per-website quota usage tracking model."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seo_indexing_tracker.models.base import Base

if TYPE_CHECKING:
    from seo_indexing_tracker.models.website import Website


class QuotaUsage(Base):
    """Tracks API usage counters by website and calendar day."""

    __tablename__ = "quota_usages"
    __table_args__ = (
        UniqueConstraint("website_id", "date", name="uq_quota_usages_website_id_date"),
        Index("ix_quota_usages_website_id_date", "website_id", "date"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    website_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("websites.id", ondelete="CASCADE"),
        nullable=False,
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    indexing_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    inspection_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    website: Mapped[Website] = relationship(back_populates="quota_usages")


__all__ = ["QuotaUsage"]
