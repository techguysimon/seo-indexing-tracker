"""Pydantic schemas for service account resources."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ServiceAccountBase(BaseModel):
    """Shared service account fields."""

    name: str = Field(min_length=1, max_length=255)
    credentials_path: str = Field(min_length=1, max_length=1024)
    scopes: list[str] = Field(default_factory=list)


class ServiceAccountCreate(ServiceAccountBase):
    """Payload used to create a service account."""

    website_id: UUID


class ServiceAccountUpdate(BaseModel):
    """Payload used to update mutable service account fields."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    credentials_path: str | None = Field(default=None, min_length=1, max_length=1024)
    scopes: list[str] | None = None


class ServiceAccountRead(ServiceAccountBase):
    """Serialized service account resource."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    website_id: UUID
    created_at: datetime


__all__ = [
    "ServiceAccountBase",
    "ServiceAccountCreate",
    "ServiceAccountRead",
    "ServiceAccountUpdate",
]
