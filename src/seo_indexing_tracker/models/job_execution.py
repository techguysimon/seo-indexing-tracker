"""Job execution ORM model for scheduler run observability."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from seo_indexing_tracker.models.base import Base


class JobExecution(Base):
    """Execution records for scheduler jobs and resumable checkpoints."""

    __tablename__ = "job_executions"
    __table_args__ = (
        Index("ix_job_executions_job_id_started_at", "job_id", "started_at"),
        Index("ix_job_executions_website_id_status", "website_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    job_id: Mapped[str] = mapped_column(String(128), nullable=False)
    job_name: Mapped[str] = mapped_column(String(255), nullable=False)
    website_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("websites.id", ondelete="SET NULL"),
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    urls_processed: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    error_message: Mapped[str | None] = mapped_column(String(2048))
    checkpoint_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)


__all__ = ["JobExecution"]
