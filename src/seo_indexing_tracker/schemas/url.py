"""Pydantic schemas for URL resources."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field


class URLBase(BaseModel):
    """Shared URL fields."""

    url: AnyHttpUrl
    lastmod: datetime | None = None
    changefreq: str | None = Field(default=None, min_length=1, max_length=32)
    sitemap_priority: float | None = Field(default=None, ge=0.0, le=1.0)


class URLCreate(URLBase):
    """Payload used to create a URL."""

    website_id: UUID
    sitemap_id: UUID | None = None
    current_priority: float | None = Field(default=None, ge=0.0, le=1.0)
    manual_priority_override: float | None = Field(default=None, ge=0.0, le=1.0)


class URLUpdate(BaseModel):
    """Payload used to update mutable URL fields."""

    sitemap_id: UUID | None = None
    url: AnyHttpUrl | None = None
    lastmod: datetime | None = None
    changefreq: str | None = Field(default=None, min_length=1, max_length=32)
    sitemap_priority: float | None = Field(default=None, ge=0.0, le=1.0)
    current_priority: float | None = Field(default=None, ge=0.0, le=1.0)
    manual_priority_override: float | None = Field(default=None, ge=0.0, le=1.0)


class URLRead(URLBase):
    """Serialized URL resource."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    website_id: UUID
    sitemap_id: UUID | None = None
    current_priority: float
    manual_priority_override: float | None = None
    discovered_at: datetime
    updated_at: datetime


__all__ = ["URLBase", "URLCreate", "URLRead", "URLUpdate"]
