"""Website ORM model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seo_indexing_tracker.models.base import Base

if TYPE_CHECKING:
    from seo_indexing_tracker.models.quota_usage import QuotaUsage
    from seo_indexing_tracker.models.rate_limit_state import RateLimitState
    from seo_indexing_tracker.models.service_account import ServiceAccount
    from seo_indexing_tracker.models.sitemap import Sitemap
    from seo_indexing_tracker.models.url import URL


class Website(Base):
    """Tracked website configuration."""

    __tablename__ = "websites"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    domain: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    site_url: Mapped[str] = mapped_column(String(500), nullable=False)
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
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rate_limit_bucket_size: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=10,
        server_default=text("10"),
    )
    rate_limit_refill_rate: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=1.0,
        server_default=text("1.0"),
    )
    rate_limit_max_concurrent_requests: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=2,
        server_default=text("2"),
    )
    rate_limit_queue_excess_requests: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("1"),
    )

    service_account: Mapped[ServiceAccount | None] = relationship(
        back_populates="website",
        uselist=False,
        cascade="all, delete-orphan",
        single_parent=True,
    )
    sitemaps: Mapped[list[Sitemap]] = relationship(
        back_populates="website",
        cascade="all, delete-orphan",
    )
    urls: Mapped[list[URL]] = relationship(
        back_populates="website",
        cascade="all, delete-orphan",
    )
    quota_usages: Mapped[list[QuotaUsage]] = relationship(
        back_populates="website",
        cascade="all, delete-orphan",
    )
    rate_limit_state: Mapped[RateLimitState | None] = relationship(
        back_populates="website",
        uselist=False,
        cascade="all, delete-orphan",
        single_parent=True,
    )


__all__ = ["Website"]
