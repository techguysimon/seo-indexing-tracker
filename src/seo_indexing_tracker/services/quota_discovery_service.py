"""Adaptive quota discovery based on observed Google API responses."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from math import ceil
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.models import QuotaDiscoveryStatus, QuotaUsage, Website
from seo_indexing_tracker.services.activity_service import ActivityService


class QuotaDiscoveryService:
    """Discover and refine practical quota limits for each website."""

    DEFAULT_INDEXING_QUOTA = 50
    DEFAULT_INSPECTION_QUOTA = 500
    CONFIDENCE_THRESHOLD_CONFIRMED = 0.95
    SUCCESS_WINDOW_FOR_ESTIMATED = 10
    SUCCESS_WINDOW_FOR_CONFIRMED = 50
    REDISCOVERY_INTERVAL = timedelta(days=7)

    def __init__(self) -> None:
        self._activity_service = ActivityService()

    async def discover_quota(self, session: AsyncSession, website_id: UUID) -> None:
        """Initialize or re-start quota discovery for a website."""

        website = await self._get_website_or_raise(
            session=session, website_id=website_id
        )
        now = datetime.now(UTC)
        website.discovered_indexing_quota = (
            website.discovered_indexing_quota or self.DEFAULT_INDEXING_QUOTA
        )
        website.discovered_inspection_quota = (
            website.discovered_inspection_quota or self.DEFAULT_INSPECTION_QUOTA
        )
        website.quota_discovery_status = QuotaDiscoveryStatus.DISCOVERING
        website.quota_discovery_confidence = max(
            float(website.quota_discovery_confidence),
            0.1,
        )
        website.quota_discovered_at = now
        await session.flush()
        await self._activity_service.log_activity(
            session=session,
            event_type="quota_discovered",
            website_id=website.id,
            resource_type="website",
            resource_id=website.id,
            message=f"Quota discovery started for {website.domain}",
            metadata={
                "indexing_quota": website.discovered_indexing_quota,
                "inspection_quota": website.discovered_inspection_quota,
            },
        )

    async def record_429(
        self,
        session: AsyncSession,
        website_id: UUID,
        api_type: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        """Record quota pressure and tune discovered limits downward."""

        website = await self._get_website_or_raise(
            session=session, website_id=website_id
        )
        normalized_api = self._normalize_api_type(api_type)
        now = datetime.now(UTC)

        current_quota = self._quota_value_for_api(
            website=website, api_type=normalized_api
        )
        floor = self._default_quota_for_api(normalized_api)
        reduced_quota = max(floor, int(current_quota * 0.9))
        self._set_quota_value_for_api(
            website=website,
            api_type=normalized_api,
            quota=max(reduced_quota, 1),
        )

        confidence_penalty = 0.15 if retry_after_seconds is not None else 0.25
        website.quota_discovery_confidence = max(
            0.05,
            float(website.quota_discovery_confidence) - confidence_penalty,
        )
        website.quota_discovery_status = (
            QuotaDiscoveryStatus.FAILED
            if website.quota_discovery_confidence < 0.15
            else QuotaDiscoveryStatus.ESTIMATED
        )
        website.quota_last_429_at = now
        website.quota_discovered_at = now
        await session.flush()

    async def record_success(
        self,
        session: AsyncSession,
        website_id: UUID,
        api_type: str,
    ) -> None:
        """Record successful API usage and improve confidence over time."""

        website = await self._get_website_or_raise(
            session=session, website_id=website_id
        )
        normalized_api = self._normalize_api_type(api_type)
        now = datetime.now(UTC)

        if self._should_restart_discovery(website=website, now=now):
            await self.discover_quota(session=session, website_id=website_id)
            return

        success_count = await self._daily_usage_for_api(
            session=session,
            website_id=website_id,
            api_type=normalized_api,
            usage_date=now.date(),
        )

        if success_count > 0 and success_count % self.SUCCESS_WINDOW_FOR_ESTIMATED == 0:
            current_quota = self._quota_value_for_api(
                website=website,
                api_type=normalized_api,
            )
            increased_quota = int(ceil(current_quota * 1.1))
            self._set_quota_value_for_api(
                website=website,
                api_type=normalized_api,
                quota=max(increased_quota, current_quota),
            )

        confidence_step = (
            0.02 if success_count < self.SUCCESS_WINDOW_FOR_ESTIMATED else 0.01
        )
        website.quota_discovery_confidence = min(
            1.0,
            float(website.quota_discovery_confidence) + confidence_step,
        )
        website.quota_discovered_at = now

        if (
            success_count >= self.SUCCESS_WINDOW_FOR_CONFIRMED
            and website.quota_discovery_confidence
            >= self.CONFIDENCE_THRESHOLD_CONFIRMED
        ):
            website.quota_discovery_status = QuotaDiscoveryStatus.CONFIRMED
        elif success_count >= self.SUCCESS_WINDOW_FOR_ESTIMATED:
            website.quota_discovery_status = QuotaDiscoveryStatus.ESTIMATED
        else:
            website.quota_discovery_status = QuotaDiscoveryStatus.DISCOVERING

        await session.flush()

    async def get_discovered_limits(
        self,
        session: AsyncSession,
        website_id: UUID,
    ) -> dict[str, object]:
        """Return discovered limit values and current confidence metadata."""

        website = await self._get_website_or_raise(
            session=session, website_id=website_id
        )
        usage = await self._usage_row(
            session=session,
            website_id=website_id,
            usage_date=datetime.now(UTC).date(),
        )
        indexing_used = int(usage.indexing_count) if usage is not None else 0
        inspection_used = int(usage.inspection_count) if usage is not None else 0
        indexing_limit = (
            website.discovered_indexing_quota or self.DEFAULT_INDEXING_QUOTA
        )
        inspection_limit = (
            website.discovered_inspection_quota or self.DEFAULT_INSPECTION_QUOTA
        )
        return {
            "indexing": {
                "used": indexing_used,
                "limit": int(indexing_limit),
                "remaining": max(int(indexing_limit) - indexing_used, 0),
            },
            "inspection": {
                "used": inspection_used,
                "limit": int(inspection_limit),
                "remaining": max(int(inspection_limit) - inspection_used, 0),
            },
            "status": website.quota_discovery_status.value,
            "confidence": float(website.quota_discovery_confidence),
            "discovered_at": website.quota_discovered_at.isoformat()
            if website.quota_discovered_at is not None
            else None,
            "last_429_at": website.quota_last_429_at.isoformat()
            if website.quota_last_429_at is not None
            else None,
        }

    async def _get_website_or_raise(
        self,
        *,
        session: AsyncSession,
        website_id: UUID,
    ) -> Website:
        website = await session.get(Website, website_id)
        if website is None:
            raise ValueError(f"Website {website_id} does not exist")
        return website

    async def _daily_usage_for_api(
        self,
        *,
        session: AsyncSession,
        website_id: UUID,
        api_type: str,
        usage_date: date,
    ) -> int:
        usage = await self._usage_row(
            session=session,
            website_id=website_id,
            usage_date=usage_date,
        )
        if usage is None:
            return 0
        if api_type == "indexing":
            return int(usage.indexing_count)
        return int(usage.inspection_count)

    async def _usage_row(
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

    @staticmethod
    def _normalize_api_type(api_type: str) -> str:
        normalized_api = api_type.strip().lower()
        if normalized_api in {"indexing", "inspection"}:
            return normalized_api
        raise ValueError("api_type must be one of: indexing, inspection")

    def _should_restart_discovery(self, *, website: Website, now: datetime) -> bool:
        if website.quota_discovery_status in {
            QuotaDiscoveryStatus.PENDING,
            QuotaDiscoveryStatus.FAILED,
        }:
            return True
        if website.quota_discovered_at is None:
            return True
        discovered_at = website.quota_discovered_at
        if discovered_at.tzinfo is None:
            discovered_at = discovered_at.replace(tzinfo=UTC)
        return now - discovered_at >= self.REDISCOVERY_INTERVAL

    def _quota_value_for_api(self, *, website: Website, api_type: str) -> int:
        if api_type == "indexing":
            return int(website.discovered_indexing_quota or self.DEFAULT_INDEXING_QUOTA)
        return int(website.discovered_inspection_quota or self.DEFAULT_INSPECTION_QUOTA)

    @staticmethod
    def _set_quota_value_for_api(
        *, website: Website, api_type: str, quota: int
    ) -> None:
        if api_type == "indexing":
            website.discovered_indexing_quota = quota
            return
        website.discovered_inspection_quota = quota

    def _default_quota_for_api(self, api_type: str) -> int:
        if api_type == "indexing":
            return self.DEFAULT_INDEXING_QUOTA
        return self.DEFAULT_INSPECTION_QUOTA


__all__ = ["QuotaDiscoveryService"]
