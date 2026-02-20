"""Per-website daily quota tracking service."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.config import get_settings
from seo_indexing_tracker.models import QuotaUsage, Website

SessionScopeFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class QuotaAPIType(str, Enum):
    """Supported API families for quota accounting."""

    INDEXING = "indexing"
    INSPECTION = "inspection"


class QuotaSettings(Protocol):
    """Settings contract used by the quota service."""

    INDEXING_DAILY_QUOTA_LIMIT: int
    INSPECTION_DAILY_QUOTA_LIMIT: int


@dataclass
class QuotaServiceSettings:
    """Concrete settings payload for service overrides and tests."""

    INDEXING_DAILY_QUOTA_LIMIT: int = 200
    INSPECTION_DAILY_QUOTA_LIMIT: int = 2000


class DailyQuotaExceededError(RuntimeError):
    """Raised when an increment would exceed the configured daily quota."""


class QuotaService:
    """Tracks and enforces daily per-website usage limits for Google APIs."""

    def __init__(
        self,
        *,
        session_factory: SessionScopeFactory | None = None,
        settings: QuotaSettings | None = None,
        today_factory: Callable[[], date] | None = None,
    ) -> None:
        if session_factory is None:
            from seo_indexing_tracker.database import session_scope

            session_factory = session_scope

        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self._today_factory = today_factory or self._default_today

    async def increment_usage(self, website_id: UUID, api_type: str) -> int:
        """Increment usage for the website and return remaining quota."""

        parsed_api_type = self._parse_api_type(api_type)
        usage_date = self._today_factory()
        counter_name = self._counter_name(parsed_api_type)
        quota_limit = self._quota_limit(parsed_api_type)

        async with self._session_factory() as session:
            await self._require_website(session=session, website_id=website_id)
            usage = await self._get_or_create_usage_row(
                session=session,
                website_id=website_id,
                usage_date=usage_date,
            )

            current_usage = getattr(usage, counter_name)
            if current_usage >= quota_limit:
                raise DailyQuotaExceededError(
                    f"Daily {parsed_api_type.value} quota exhausted for website {website_id}"
                )

            updated_usage = current_usage + 1
            setattr(usage, counter_name, updated_usage)
            return int(quota_limit - updated_usage)

    async def get_remaining_quota(self, website_id: UUID, api_type: str) -> int:
        """Return remaining daily quota for a website and API type."""

        parsed_api_type = self._parse_api_type(api_type)
        usage_date = self._today_factory()
        counter_name = self._counter_name(parsed_api_type)
        quota_limit = self._quota_limit(parsed_api_type)

        async with self._session_factory() as session:
            await self._require_website(session=session, website_id=website_id)
            usage = await self._get_usage_row(
                session=session,
                website_id=website_id,
                usage_date=usage_date,
            )
            if usage is None:
                return quota_limit

            used = getattr(usage, counter_name)
            return int(max(quota_limit - used, 0))

    async def check_quota_available(
        self,
        website_id: UUID,
        api_type: str,
        count: int,
    ) -> bool:
        """Check whether at least ``count`` quota units are still available."""

        if count <= 0:
            raise ValueError("count must be greater than zero")

        remaining_quota = await self.get_remaining_quota(website_id, api_type)
        return remaining_quota >= count

    async def _require_website(
        self, *, session: AsyncSession, website_id: UUID
    ) -> None:
        website = await session.get(Website, website_id)
        if website is None:
            raise ValueError(f"Website {website_id} does not exist")

    async def _get_usage_row(
        self,
        *,
        session: AsyncSession,
        website_id: UUID,
        usage_date: date,
    ) -> QuotaUsage | None:
        result = await session.execute(
            select(QuotaUsage).where(
                QuotaUsage.website_id == website_id,
                QuotaUsage.date == usage_date,
            )
        )
        return result.scalar_one_or_none()

    async def _get_or_create_usage_row(
        self,
        *,
        session: AsyncSession,
        website_id: UUID,
        usage_date: date,
    ) -> QuotaUsage:
        usage = await self._get_usage_row(
            session=session,
            website_id=website_id,
            usage_date=usage_date,
        )
        if usage is not None:
            return usage

        usage = QuotaUsage(
            website_id=website_id,
            date=usage_date,
            indexing_count=0,
            inspection_count=0,
        )
        session.add(usage)
        await session.flush()
        return usage

    @staticmethod
    def _default_today() -> date:
        return datetime.now(UTC).date()

    @staticmethod
    def _parse_api_type(api_type: str) -> QuotaAPIType:
        try:
            return QuotaAPIType(api_type.lower())
        except ValueError as error:
            valid_values = ", ".join(api.value for api in QuotaAPIType)
            raise ValueError(
                f"Unsupported api_type '{api_type}'. Expected one of: {valid_values}"
            ) from error

    def _quota_limit(self, api_type: QuotaAPIType) -> int:
        if api_type is QuotaAPIType.INDEXING:
            return self._settings.INDEXING_DAILY_QUOTA_LIMIT

        return self._settings.INSPECTION_DAILY_QUOTA_LIMIT

    @staticmethod
    def _counter_name(api_type: QuotaAPIType) -> str:
        if api_type is QuotaAPIType.INDEXING:
            return "indexing_count"

        return "inspection_count"


__all__ = [
    "QuotaAPIType",
    "DailyQuotaExceededError",
    "QuotaService",
    "QuotaServiceSettings",
]
