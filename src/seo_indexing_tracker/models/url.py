"""URL ORM model with priority tracking."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seo_indexing_tracker.models.base import Base

DEFAULT_URL_PRIORITY = 0.5

if TYPE_CHECKING:
    from seo_indexing_tracker.models.index_status import IndexStatus
    from seo_indexing_tracker.models.submission_log import SubmissionLog
    from seo_indexing_tracker.models.sitemap import Sitemap
    from seo_indexing_tracker.models.website import Website


def _calculate_default_current_priority(context: Any) -> float:
    current_parameters = context.get_current_parameters()
    sitemap_priority = current_parameters.get("sitemap_priority")
    if sitemap_priority is None:
        return DEFAULT_URL_PRIORITY
    return float(sitemap_priority)


class URLIndexStatus(str, Enum):
    """Denormalized latest index state used for fast URL filtering."""

    INDEXED = "INDEXED"
    NOT_INDEXED = "NOT_INDEXED"
    BLOCKED = "BLOCKED"
    SOFT_404 = "SOFT_404"
    ERROR = "ERROR"
    UNCHECKED = "UNCHECKED"


class URL(Base):
    """Tracked URL discovered from sitemaps or manual input."""

    __tablename__ = "urls"
    __table_args__ = (
        UniqueConstraint("website_id", "url", name="uq_urls_website_id_url"),
        CheckConstraint(
            "sitemap_priority IS NULL OR (sitemap_priority >= 0 AND sitemap_priority <= 1)",
            name="ck_urls_sitemap_priority_range",
        ),
        CheckConstraint(
            "current_priority >= 0 AND current_priority <= 1",
            name="ck_urls_current_priority_range",
        ),
        CheckConstraint(
            "manual_priority_override IS NULL OR (manual_priority_override >= 0 AND manual_priority_override <= 1)",
            name="ck_urls_manual_priority_override_range",
        ),
        Index("ix_urls_website_id", "website_id"),
        Index(
            "ix_urls_website_id_latest_index_status",
            "website_id",
            "latest_index_status",
        ),
        Index("ix_urls_current_priority", "current_priority"),
        Index("ix_urls_updated_at", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    website_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("websites.id", ondelete="CASCADE"),
        nullable=False,
    )
    sitemap_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sitemaps.id", ondelete="SET NULL"),
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    lastmod: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    changefreq: Mapped[str | None] = mapped_column(String(32))
    sitemap_priority: Mapped[float | None] = mapped_column()
    current_priority: Mapped[float] = mapped_column(
        nullable=False,
        default=_calculate_default_current_priority,
        server_default=text(str(DEFAULT_URL_PRIORITY)),
    )
    manual_priority_override: Mapped[float | None] = mapped_column()
    latest_index_status: Mapped[URLIndexStatus] = mapped_column(
        SqlEnum(URLIndexStatus, name="url_index_status"),
        nullable=False,
        default=URLIndexStatus.UNCHECKED,
        server_default=URLIndexStatus.UNCHECKED.value,
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[datetime] = mapped_column(
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

    website: Mapped[Website] = relationship(back_populates="urls")
    sitemap: Mapped[Sitemap | None] = relationship(back_populates="urls")
    index_statuses: Mapped[list[IndexStatus]] = relationship(
        back_populates="url",
        cascade="all, delete-orphan",
    )
    submission_logs: Mapped[list[SubmissionLog]] = relationship(
        back_populates="url",
        cascade="all, delete-orphan",
    )


__all__ = ["DEFAULT_URL_PRIORITY", "URL", "URLIndexStatus"]
