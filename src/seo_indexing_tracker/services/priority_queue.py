"""Priority queue management backed by URL priority columns."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, TypeGuard, cast
from uuid import UUID

from sqlalchemy import bindparam, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.models.url import DEFAULT_URL_PRIORITY, URL

DEFAULT_BATCH_SIZE = 500
FRESHNESS_WINDOW_DAYS = 30
SITEMAP_WEIGHT = 0.7
FRESHNESS_WEIGHT = 0.3

SessionScopeFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_MANUAL_OVERRIDE_UNSET: object = object()


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)

    return value.astimezone(UTC)


def _clamp_priority(value: float) -> float:
    return max(0.0, min(1.0, value))


def _freshness_score(*, lastmod: datetime | None, now: datetime) -> float:
    normalized_lastmod = _normalize_datetime(lastmod)
    if normalized_lastmod is None:
        return 0.5

    age_seconds = (now - normalized_lastmod).total_seconds()
    if age_seconds <= 0:
        return 1.0

    age_days = age_seconds / 86400
    decayed_score = 1.0 - min(age_days, FRESHNESS_WINDOW_DAYS) / FRESHNESS_WINDOW_DAYS
    return _clamp_priority(decayed_score)


def _is_numeric_priority(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float)


def calculate_url_priority(
    *,
    lastmod: datetime | None,
    sitemap_priority: float | None,
    manual_override: float | None,
    now: datetime | None = None,
) -> float:
    """Calculate URL queue priority using manual override, sitemap data, and freshness."""

    if manual_override is not None:
        return _clamp_priority(float(manual_override))

    normalized_now = _normalize_datetime(now) or datetime.now(UTC)
    base_priority = (
        DEFAULT_URL_PRIORITY
        if sitemap_priority is None
        else _clamp_priority(float(sitemap_priority))
    )
    freshness_score = _freshness_score(lastmod=lastmod, now=normalized_now)
    weighted_priority = (base_priority * SITEMAP_WEIGHT) + (
        freshness_score * FRESHNESS_WEIGHT
    )
    return round(_clamp_priority(weighted_priority), 6)


class PriorityQueueService:
    """Manage enqueue/dequeue operations using URL.current_priority."""

    def __init__(
        self,
        *,
        session_factory: SessionScopeFactory | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")

        if session_factory is None:
            from seo_indexing_tracker.database import session_scope

            session_factory = session_scope

        self._session_factory = session_factory
        self._batch_size = batch_size

    async def enqueue(self, url_id: UUID) -> URL:
        """Calculate and persist priority for a single URL."""

        async with self._session_factory() as session:
            url = await session.get(URL, url_id)
            if url is None:
                raise ValueError(f"URL {url_id} does not exist")

            url.current_priority = calculate_url_priority(
                lastmod=url.lastmod,
                sitemap_priority=url.sitemap_priority,
                manual_override=url.manual_priority_override,
            )
            return url

    async def enqueue_many(self, url_ids: Sequence[UUID]) -> int:
        """Calculate and persist priorities for multiple URLs in batches."""

        if not url_ids:
            return 0

        async with self._session_factory() as session:
            urls_result = await session.execute(select(URL).where(URL.id.in_(url_ids)))
            urls = urls_result.scalars().all()
            if len(urls) != len(set(url_ids)):
                existing_ids = {url.id for url in urls}
                missing_ids = [
                    url_id for url_id in set(url_ids) if url_id not in existing_ids
                ]
                missing_ids_text = ", ".join(
                    str(url_id) for url_id in sorted(missing_ids)
                )
                raise ValueError(f"Cannot enqueue missing URL ids: {missing_ids_text}")

            updates = [
                {
                    "b_id": url.id,
                    "current_priority": calculate_url_priority(
                        lastmod=url.lastmod,
                        sitemap_priority=url.sitemap_priority,
                        manual_override=url.manual_priority_override,
                    ),
                }
                for url in urls
            ]

            url_table = cast(Any, URL.__table__)
            for start_index in range(0, len(updates), self._batch_size):
                batch = updates[start_index : start_index + self._batch_size]
                await session.execute(
                    update(url_table)
                    .where(url_table.c.id == bindparam("b_id"))
                    .values(current_priority=bindparam("current_priority"))
                    .execution_options(synchronize_session=False),
                    batch,
                )

            return len(updates)

    async def peek(self, website_id: UUID, *, limit: int) -> list[URL]:
        """Read queued URLs ordered by highest priority first."""

        if limit <= 0:
            raise ValueError("limit must be greater than zero")

        async with self._session_factory() as session:
            result = await session.execute(
                select(URL)
                .where(URL.website_id == website_id, URL.current_priority > 0)
                .order_by(URL.current_priority.desc(), URL.updated_at.asc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def dequeue(self, website_id: UUID, *, limit: int) -> list[URL]:
        """Pop queued URLs by priority and mark them as processed."""

        if limit <= 0:
            raise ValueError("limit must be greater than zero")

        async with self._session_factory() as session:
            result = await session.execute(
                select(URL)
                .where(URL.website_id == website_id, URL.current_priority > 0)
                .order_by(URL.current_priority.desc(), URL.updated_at.asc())
                .limit(limit)
            )
            urls = list(result.scalars().all())
            if not urls:
                return []

            processed_rows = [{"b_id": url.id, "current_priority": 0.0} for url in urls]
            for url in urls:
                url.current_priority = 0.0

            url_table = cast(Any, URL.__table__)
            await session.execute(
                update(url_table)
                .where(url_table.c.id == bindparam("b_id"))
                .values(current_priority=bindparam("current_priority"))
                .execution_options(synchronize_session=False),
                processed_rows,
            )
            return urls

    async def reprioritize(
        self,
        url_id: UUID,
        *,
        manual_override: float | None | object = _MANUAL_OVERRIDE_UNSET,
    ) -> URL:
        """Recalculate a URL priority, optionally setting a manual override."""

        async with self._session_factory() as session:
            url = await session.get(URL, url_id)
            if url is None:
                raise ValueError(f"URL {url_id} does not exist")

            if manual_override is not _MANUAL_OVERRIDE_UNSET:
                if manual_override is None:
                    url.manual_priority_override = None
                elif not _is_numeric_priority(manual_override):
                    raise TypeError("manual_override must be a float, None, or unset")
                else:
                    manual_override_value = _clamp_priority(float(manual_override))
                    url.manual_priority_override = manual_override_value

            url.current_priority = calculate_url_priority(
                lastmod=url.lastmod,
                sitemap_priority=url.sitemap_priority,
                manual_override=url.manual_priority_override,
            )
            return url

    async def remove(self, url_id: UUID) -> URL:
        """Remove a URL from the active queue."""

        async with self._session_factory() as session:
            url = await session.get(URL, url_id)
            if url is None:
                raise ValueError(f"URL {url_id} does not exist")

            url.current_priority = 0.0
            url.manual_priority_override = None
            return url


__all__ = ["PriorityQueueService", "calculate_url_priority"]
