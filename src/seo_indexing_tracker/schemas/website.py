"""Pydantic schemas for website resources."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from seo_indexing_tracker.schemas.service_account import ServiceAccountRead


class WebsiteBase(BaseModel):
    """Shared website fields."""

    domain: str = Field(min_length=1, max_length=255)
    site_url: str = Field(
        description="Site URL for Google API - either https://domain.com for URL-prefix properties or sc-domain:domain.com for Domain properties"
    )
    is_active: bool = True
    rate_limit_bucket_size: int = Field(default=10, ge=1)
    rate_limit_refill_rate: float = Field(default=1.0, gt=0)
    rate_limit_max_concurrent_requests: int = Field(default=2, ge=1)
    rate_limit_queue_excess_requests: bool = True


class WebsiteCreate(WebsiteBase):
    """Payload used to create a website."""


class WebsiteUpdate(BaseModel):
    """Payload used to update mutable website fields."""

    domain: str | None = Field(default=None, min_length=1, max_length=255)
    site_url: str | None = Field(
        default=None,
        description="Site URL for Google API - either https://domain.com for URL-prefix properties or sc-domain:domain.com for Domain properties",
    )
    is_active: bool | None = None
    rate_limit_bucket_size: int | None = Field(default=None, ge=1)
    rate_limit_refill_rate: float | None = Field(default=None, gt=0)
    rate_limit_max_concurrent_requests: int | None = Field(default=None, ge=1)
    rate_limit_queue_excess_requests: bool | None = None


class WebsiteRateLimitUpdate(BaseModel):
    """Payload used to update website rate limiting settings."""

    rate_limit_bucket_size: int | None = Field(default=None, ge=1)
    rate_limit_refill_rate: float | None = Field(default=None, gt=0)
    rate_limit_max_concurrent_requests: int | None = Field(default=None, ge=1)
    rate_limit_queue_excess_requests: bool | None = None


class WebsiteRateLimitRead(BaseModel):
    """Serialized website rate limiting settings."""

    model_config = ConfigDict(from_attributes=True)

    website_id: UUID
    rate_limit_bucket_size: int
    rate_limit_refill_rate: float
    rate_limit_max_concurrent_requests: int
    rate_limit_queue_excess_requests: bool


class WebsiteRead(WebsiteBase):
    """Serialized website resource."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime
    service_account: ServiceAccountRead | None = None


class WebsiteDetailRead(WebsiteRead):
    """Serialized website resource with aggregate relationship counts."""

    service_account_count: int
    sitemap_count: int


__all__ = [
    "WebsiteBase",
    "WebsiteCreate",
    "WebsiteDetailRead",
    "WebsiteRead",
    "WebsiteRateLimitRead",
    "WebsiteRateLimitUpdate",
    "WebsiteUpdate",
]
