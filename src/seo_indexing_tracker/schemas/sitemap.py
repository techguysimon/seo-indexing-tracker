"""Pydantic schemas for sitemap resources."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict

from seo_indexing_tracker.models.sitemap import SitemapType


class SitemapBase(BaseModel):
    """Shared sitemap fields."""

    url: AnyHttpUrl
    sitemap_type: SitemapType
    is_active: bool = True


class SitemapCreate(BaseModel):
    """Payload used to create a sitemap."""

    url: AnyHttpUrl
    sitemap_type: SitemapType | None = None
    is_active: bool = True


class SitemapUpdate(BaseModel):
    """Payload used to update mutable sitemap fields."""

    url: AnyHttpUrl | None = None
    sitemap_type: SitemapType | None = None
    last_fetched: datetime | None = None
    etag: str | None = None
    last_modified_header: str | None = None
    is_active: bool | None = None


class SitemapRead(SitemapBase):
    """Serialized sitemap resource."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    website_id: UUID
    last_fetched: datetime | None = None
    etag: str | None = None
    last_modified_header: str | None = None
    created_at: datetime


__all__ = ["SitemapBase", "SitemapCreate", "SitemapRead", "SitemapUpdate"]
