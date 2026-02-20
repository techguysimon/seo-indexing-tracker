"""Pydantic schemas for index status resources."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

from seo_indexing_tracker.models.index_status import IndexVerdict


class IndexStatusBase(BaseModel):
    """Shared index status fields."""

    coverage_state: str = Field(min_length=1, max_length=255)
    verdict: IndexVerdict
    last_crawl_time: datetime | None = None
    indexed_at: datetime | None = None
    checked_at: datetime | None = None
    robots_txt_state: str | None = Field(default=None, min_length=1, max_length=255)
    indexing_state: str | None = Field(default=None, min_length=1, max_length=255)
    page_fetch_state: str | None = Field(default=None, min_length=1, max_length=255)
    google_canonical: AnyHttpUrl | None = None
    user_canonical: AnyHttpUrl | None = None
    raw_response: dict[str, Any]


class IndexStatusCreate(IndexStatusBase):
    """Payload used to create an index status record."""

    url_id: UUID


class IndexStatusUpdate(BaseModel):
    """Payload used to update mutable index status fields."""

    coverage_state: str | None = Field(default=None, min_length=1, max_length=255)
    verdict: IndexVerdict | None = None
    last_crawl_time: datetime | None = None
    indexed_at: datetime | None = None
    checked_at: datetime | None = None
    robots_txt_state: str | None = Field(default=None, min_length=1, max_length=255)
    indexing_state: str | None = Field(default=None, min_length=1, max_length=255)
    page_fetch_state: str | None = Field(default=None, min_length=1, max_length=255)
    google_canonical: AnyHttpUrl | None = None
    user_canonical: AnyHttpUrl | None = None
    raw_response: dict[str, Any] | None = None


class IndexStatusRead(IndexStatusBase):
    """Serialized index status resource."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    url_id: UUID
    checked_at: datetime


__all__ = [
    "IndexStatusBase",
    "IndexStatusCreate",
    "IndexStatusRead",
    "IndexStatusUpdate",
]
