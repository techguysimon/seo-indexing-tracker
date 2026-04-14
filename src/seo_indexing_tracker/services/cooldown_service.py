"""Cooldown window logic for submission rate limiting."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from seo_indexing_tracker.config import Settings
from seo_indexing_tracker.models import Website

DEFAULT_INDEXED_REVERIFICATION_MIN_AGE_SECONDS = 7 * 24 * 60 * 60


@dataclass(slots=True, frozen=True)
class _SubmissionCooldownWindow:
    website_id: UUID
    domain: str
    last_429_at: datetime
    cooldown_seconds: int
    next_allowed_at: datetime
    is_internal_rate_limit: bool = False


class CooldownService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def get_cooldown_window(self, website: Website) -> _SubmissionCooldownWindow | None:
        cooldown_seconds = int(self._settings.QUOTA_RATE_LIMIT_COOLDOWN_SECONDS)
        if cooldown_seconds <= 0:
            return None

        now = datetime.now(UTC)

        if website.quota_last_429_at is not None:
            normalized_last_429_at = website.quota_last_429_at
            if normalized_last_429_at.tzinfo is None:
                normalized_last_429_at = normalized_last_429_at.replace(tzinfo=UTC)

            next_allowed_at = normalized_last_429_at + timedelta(
                seconds=cooldown_seconds
            )
            if now < next_allowed_at:
                return _SubmissionCooldownWindow(
                    website_id=website.id,
                    domain=website.domain,
                    last_429_at=normalized_last_429_at,
                    cooldown_seconds=cooldown_seconds,
                    next_allowed_at=next_allowed_at,
                    is_internal_rate_limit=False,
                )

        if website.internal_rate_limit_at is not None:
            normalized_internal_at = website.internal_rate_limit_at
            if normalized_internal_at.tzinfo is None:
                normalized_internal_at = normalized_internal_at.replace(tzinfo=UTC)

            next_allowed_at = normalized_internal_at + timedelta(
                seconds=cooldown_seconds
            )
            if now < next_allowed_at:
                return _SubmissionCooldownWindow(
                    website_id=website.id,
                    domain=website.domain,
                    last_429_at=normalized_internal_at,
                    cooldown_seconds=cooldown_seconds,
                    next_allowed_at=next_allowed_at,
                    is_internal_rate_limit=True,
                )

        return None

    def get_indexed_reverification_min_age(self) -> int:
        configured_age_seconds = getattr(
            self._settings,
            "SCHEDULER_INDEXED_REVERIFICATION_MIN_AGE_SECONDS",
            None,
        )
        if (
            isinstance(configured_age_seconds, int | float)
            and configured_age_seconds >= 0
        ):
            return int(configured_age_seconds)

        return DEFAULT_INDEXED_REVERIFICATION_MIN_AGE_SECONDS
