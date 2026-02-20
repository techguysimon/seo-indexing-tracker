"""Index status ORM model for URL verification history."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Index,
    JSON,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seo_indexing_tracker.models.base import Base

if TYPE_CHECKING:
    from seo_indexing_tracker.models.url import URL


class IndexVerdict(str, Enum):
    """Verification verdict returned by index inspection checks."""

    PASS = "PASS"
    FAIL = "FAIL"
    NEUTRAL = "NEUTRAL"
    PARTIAL = "PARTIAL"


class IndexStatus(Base):
    """Stored index inspection result for a tracked URL."""

    __tablename__ = "index_statuses"
    __table_args__ = (
        Index("ix_index_statuses_url_id_checked_at", "url_id", "checked_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    url_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("urls.id", ondelete="CASCADE"),
        nullable=False,
    )
    coverage_state: Mapped[str] = mapped_column(String(255), nullable=False)
    verdict: Mapped[IndexVerdict] = mapped_column(
        SqlEnum(IndexVerdict, name="index_verdict"),
        nullable=False,
    )
    last_crawl_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    robots_txt_state: Mapped[str | None] = mapped_column(String(255))
    indexing_state: Mapped[str | None] = mapped_column(String(255))
    page_fetch_state: Mapped[str | None] = mapped_column(String(255))
    google_canonical: Mapped[str | None] = mapped_column(String(2048))
    user_canonical: Mapped[str | None] = mapped_column(String(2048))
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    url: Mapped[URL] = relationship(back_populates="index_statuses")


__all__ = ["IndexStatus", "IndexVerdict"]
