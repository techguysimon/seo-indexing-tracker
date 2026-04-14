"""Queue ETA calculation service for submission and verification queues."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Literal
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.config import get_settings
from seo_indexing_tracker.models import QuotaUsage, URL, URLIndexStatus, Website

QueueStatus = Literal["active", "paused", "complete"]


@dataclass(frozen=True)
class QueueETA:
    """ETA information for a single queue."""

    queued: int
    quota_remaining: int
    quota_limit: int
    eta_minutes: int | None
    rate_per_minute: float


@dataclass(frozen=True)
class WebsiteQueueETA:
    """Complete ETA information for a website."""

    website_id: UUID
    website_domain: str
    submission_queue: QueueETA
    verification_queue: QueueETA
    quota_reset_at: datetime
    status: QueueStatus


class QueueETAService:
    """Calculate ETA for submission and verification queues per website."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._settings = get_settings()

    async def get_website_eta(self, website_id: UUID) -> WebsiteQueueETA | None:
        """Calculate ETA for a specific website."""
        website = await self._session.get(Website, website_id)
        if website is None:
            return None

        status = self._determine_status(website)

        submission_queued = await self._count_submission_queue(website_id)
        verification_queued = await self._count_verification_queue(website_id)

        quota_usage = await self._get_quota_usage(website_id)
        indexing_quota_remaining, indexing_quota_limit = self._calculate_indexing_quota(
            website, quota_usage
        )
        inspection_quota_remaining, inspection_quota_limit = (
            self._calculate_inspection_quota(website, quota_usage)
        )

        submission_eta = self._calculate_queue_eta(
            queued=submission_queued,
            quota_remaining=indexing_quota_remaining,
            quota_limit=indexing_quota_limit,
            batch_size=self._settings.SCHEDULER_URL_SUBMISSION_BATCH_SIZE,
            interval_seconds=self._settings.SCHEDULER_URL_SUBMISSION_INTERVAL_SECONDS,
            status=status,
        )

        verification_eta = self._calculate_queue_eta(
            queued=verification_queued,
            quota_remaining=inspection_quota_remaining,
            quota_limit=inspection_quota_limit,
            batch_size=self._settings.SCHEDULER_INDEX_VERIFICATION_BATCH_SIZE,
            interval_seconds=self._settings.SCHEDULER_INDEX_VERIFICATION_INTERVAL_SECONDS,
            status=status,
        )

        quota_reset_at = self._calculate_quota_reset_time()

        return WebsiteQueueETA(
            website_id=website.id,
            website_domain=website.domain,
            submission_queue=submission_eta,
            verification_queue=verification_eta,
            quota_reset_at=quota_reset_at,
            status=status,
        )

    async def get_all_websites_eta(self) -> list[WebsiteQueueETA]:
        """Calculate ETA for all active websites."""
        result = await self._session.execute(
            select(Website).where(Website.is_active.is_(True))
        )
        websites = result.scalars().all()

        etas = []
        for website in websites:
            eta = await self.get_website_eta(website.id)
            if eta is not None:
                etas.append(eta)

        return etas

    def _determine_status(self, website: Website) -> QueueStatus:
        """Determine the queue status for a website."""
        if not website.is_active:
            return "paused"
        return "active"

    async def _count_submission_queue(self, website_id: UUID) -> int:
        """Count URLs in the submission queue (current_priority > 0)."""
        result = await self._session.execute(
            select(func.count(URL.id)).where(
                URL.website_id == website_id,
                URL.current_priority > 0,
            )
        )
        return int(result.scalar() or 0)

    async def _count_verification_queue(self, website_id: UUID) -> int:
        """Count URLs needing verification or reverification (UNCHECKED + ERROR)."""
        result = await self._session.execute(
            select(func.count(URL.id)).where(
                URL.website_id == website_id,
                URL.latest_index_status.in_(
                    [URLIndexStatus.UNCHECKED, URLIndexStatus.ERROR]
                ),
            )
        )
        return int(result.scalar() or 0)

    async def _get_quota_usage(self, website_id: UUID) -> QuotaUsage | None:
        """Get today's quota usage for the website."""
        today = datetime.now(UTC).date()
        result = await self._session.execute(
            select(QuotaUsage).where(
                QuotaUsage.website_id == website_id,
                QuotaUsage.date == today,
            )
        )
        return result.scalar_one_or_none()

    def _calculate_indexing_quota(
        self, website: Website, usage: QuotaUsage | None
    ) -> tuple[int, int]:
        """Calculate remaining and limit for indexing quota."""
        limit = self._get_indexing_quota_limit(website)
        used = int(usage.indexing_count) if usage is not None else 0
        remaining = max(limit - used, 0)
        return remaining, limit

    def _calculate_inspection_quota(
        self, website: Website, usage: QuotaUsage | None
    ) -> tuple[int, int]:
        """Calculate remaining and limit for inspection quota."""
        limit = self._get_inspection_quota_limit(website)
        used = int(usage.inspection_count) if usage is not None else 0
        remaining = max(limit - used, 0)
        return remaining, limit

    def _get_indexing_quota_limit(self, website: Website) -> int:
        """Get the effective indexing quota limit for a website."""
        if website.discovered_indexing_quota is not None:
            return int(website.discovered_indexing_quota)
        return int(self._settings.INDEXING_DAILY_QUOTA_LIMIT)

    def _get_inspection_quota_limit(self, website: Website) -> int:
        """Get the effective inspection quota limit for a website."""
        if website.discovered_inspection_quota is not None:
            return int(website.discovered_inspection_quota)
        return int(self._settings.INSPECTION_DAILY_QUOTA_LIMIT)

    def _calculate_queue_eta(
        self,
        queued: int,
        quota_remaining: int,
        quota_limit: int,
        batch_size: int,
        interval_seconds: int,
        status: QueueStatus,
    ) -> QueueETA:
        """Calculate ETA for a queue using the specified formula."""
        if status == "paused":
            return QueueETA(
                queued=queued,
                quota_remaining=quota_remaining,
                quota_limit=quota_limit,
                eta_minutes=None,
                rate_per_minute=0.0,
            )

        if queued == 0:
            return QueueETA(
                queued=queued,
                quota_remaining=quota_remaining,
                quota_limit=quota_limit,
                eta_minutes=None,
                rate_per_minute=float(batch_size / (interval_seconds / 60)),
            )

        if quota_remaining == 0:
            seconds_until_midnight = self._seconds_until_midnight()
            return QueueETA(
                queued=queued,
                quota_remaining=quota_remaining,
                quota_limit=quota_limit,
                eta_minutes=ceil(seconds_until_midnight / 60),
                rate_per_minute=0.0,
            )

        urls_per_batch = min(quota_remaining, batch_size)
        batches_needed = ceil(queued / urls_per_batch)
        eta_seconds = batches_needed * interval_seconds

        rate_per_minute = batch_size / (interval_seconds / 60)

        return QueueETA(
            queued=queued,
            quota_remaining=quota_remaining,
            quota_limit=quota_limit,
            eta_minutes=ceil(eta_seconds / 60),
            rate_per_minute=rate_per_minute,
        )

    def _calculate_quota_reset_time(self) -> datetime:
        """Calculate the next midnight UTC for quota reset."""
        now = datetime.now(UTC)
        tomorrow = now.date() + timedelta(days=1)
        return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=UTC)

    def _seconds_until_midnight(self) -> int:
        """Calculate seconds until midnight UTC."""
        now = datetime.now(UTC)
        tomorrow = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(
            days=1
        )
        return int((tomorrow - now).total_seconds())


__all__ = [
    "QueueETA",
    "QueueETAService",
    "QueueStatus",
    "WebsiteQueueETA",
]
