"""Persistent token-bucket state for website rate limiting."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Float, ForeignKey, Index, Uuid, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seo_indexing_tracker.models.base import Base

if TYPE_CHECKING:
    from seo_indexing_tracker.models.website import Website


class RateLimitState(Base):
    """Stores per-website token counts and refill timestamps."""

    __tablename__ = "rate_limit_states"
    __table_args__ = (
        UniqueConstraint("website_id", name="uq_rate_limit_states_website_id"),
        Index("ix_rate_limit_states_website_id", "website_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    website_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("websites.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_count: Mapped[float] = mapped_column(Float, nullable=False)
    last_refill_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
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

    website: Mapped[Website] = relationship(back_populates="rate_limit_state")


__all__ = ["RateLimitState"]
