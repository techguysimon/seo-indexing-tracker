"""Service account ORM model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, JSON, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seo_indexing_tracker.models.base import Base

if TYPE_CHECKING:
    from seo_indexing_tracker.models.website import Website


class ServiceAccount(Base):
    """Google service account credentials mapped to a website."""

    __tablename__ = "service_accounts"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    website_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("websites.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    credentials_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    website: Mapped[Website] = relationship(back_populates="service_account")


__all__ = ["ServiceAccount"]
