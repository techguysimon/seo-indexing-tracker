"""Sitemap ORM model."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seo_indexing_tracker.models.base import Base

if TYPE_CHECKING:
    from seo_indexing_tracker.models.url import URL
    from seo_indexing_tracker.models.website import Website


class SitemapType(str, Enum):
    """Supported sitemap variants."""

    INDEX = "INDEX"
    URLSET = "URLSET"


class Sitemap(Base):
    """Discovered sitemap resource for a website."""

    __tablename__ = "sitemaps"
    __table_args__ = (
        UniqueConstraint("website_id", "url", name="uq_sitemaps_website_id_url"),
        Index("ix_sitemaps_website_id", "website_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    website_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("websites.id", ondelete="CASCADE"),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    sitemap_type: Mapped[SitemapType] = mapped_column(
        SqlEnum(SitemapType, name="sitemap_type"),
        nullable=False,
    )
    last_fetched: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    etag: Mapped[str | None] = mapped_column(String(255))
    last_modified_header: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    website: Mapped[Website] = relationship(back_populates="sitemaps")
    urls: Mapped[list[URL]] = relationship(back_populates="sitemap")


__all__ = ["Sitemap", "SitemapType"]
