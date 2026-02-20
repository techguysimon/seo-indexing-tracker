"""Pydantic schemas for submission log resources."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from seo_indexing_tracker.models.submission_log import (
    SubmissionAction,
    SubmissionStatus,
)


class SubmissionLogBase(BaseModel):
    """Shared submission log fields."""

    action: SubmissionAction
    api_response: dict[str, Any]
    submitted_at: datetime | None = None
    status: SubmissionStatus
    error_message: str | None = Field(default=None, max_length=2048)


class SubmissionLogRead(SubmissionLogBase):
    """Serialized submission log resource."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    url_id: UUID
    submitted_at: datetime


class SubmissionLogFilter(BaseModel):
    """Filter options for submission log retrieval."""

    url_id: UUID | None = None
    status: SubmissionStatus | None = None
    submitted_after: datetime | None = None
    submitted_before: datetime | None = None


__all__ = [
    "SubmissionLogBase",
    "SubmissionLogFilter",
    "SubmissionLogRead",
]
