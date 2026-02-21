"""Sitemap refresh progress ORM model for discovery observability."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from seo_indexing_tracker.models.base import Base


class SitemapRefreshProgress(Base):
    """Track refresh lifecycle and URL counters for a sitemap."""

    __tablename__ = "sitemap_refresh_progress"
    __table_args__ = (
        UniqueConstraint("sitemap_id", name="uq_sitemap_refresh_progress_sitemap_id"),
        Index("ix_sitemap_refresh_progress_website_status", "website_id", "status"),
        Index("ix_sitemap_refresh_progress_updated_at", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    sitemap_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sitemaps.id", ondelete="CASCADE"),
        nullable=False,
    )
    website_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("websites.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    urls_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    urls_new: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    urls_modified: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[str | None] = mapped_column(String(2048))


__all__ = ["SitemapRefreshProgress"]
