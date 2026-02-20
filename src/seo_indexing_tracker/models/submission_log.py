"""Submission log ORM model for indexing API audit trails."""

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


class SubmissionAction(str, Enum):
    """Action submitted to the indexing API."""

    URL_UPDATED = "URL_UPDATED"
    URL_DELETED = "URL_DELETED"


class SubmissionStatus(str, Enum):
    """Outcome status recorded for a submission."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    RATE_LIMITED = "RATE_LIMITED"


class SubmissionLog(Base):
    """Audit record for URL submission attempts."""

    __tablename__ = "submission_logs"
    __table_args__ = (
        Index("ix_submission_logs_url_id_submitted_at", "url_id", "submitted_at"),
        Index("ix_submission_logs_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    url_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("urls.id", ondelete="CASCADE"),
        nullable=False,
    )
    action: Mapped[SubmissionAction] = mapped_column(
        SqlEnum(SubmissionAction, name="submission_action"),
        nullable=False,
    )
    api_response: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    status: Mapped[SubmissionStatus] = mapped_column(
        SqlEnum(SubmissionStatus, name="submission_status"),
        nullable=False,
    )
    error_message: Mapped[str | None] = mapped_column(String(2048))

    url: Mapped[URL] = relationship(back_populates="submission_logs")


__all__ = ["SubmissionAction", "SubmissionLog", "SubmissionStatus"]
