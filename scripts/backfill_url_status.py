"""Backfill URL denormalized status columns from latest index_statuses rows."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import and_, func, select

from seo_indexing_tracker.config import get_settings
from seo_indexing_tracker.database import AsyncSessionFactory, close_database
from seo_indexing_tracker.models import IndexStatus, URL, URLIndexStatus


def _derived_status(*, status: IndexStatus) -> URLIndexStatus:
    normalized_coverage = status.coverage_state.strip().casefold()

    if normalized_coverage in {
        "indexed",
        "submitted and indexed",
    }:
        return URLIndexStatus.INDEXED

    if "soft 404" in normalized_coverage:
        return URLIndexStatus.SOFT_404

    if "blocked" in normalized_coverage or "robots" in normalized_coverage:
        return URLIndexStatus.BLOCKED

    if normalized_coverage in {"inspection_failed", "unknown", "error"}:
        return URLIndexStatus.ERROR

    return URLIndexStatus.NOT_INDEXED


async def _run_backfill() -> tuple[int, int]:
    latest_status_subquery = (
        select(
            IndexStatus.url_id.label("url_id"),
            func.max(IndexStatus.checked_at).label("checked_at"),
        )
        .group_by(IndexStatus.url_id)
        .subquery()
    )

    async with AsyncSessionFactory() as session:
        urls = list(await session.scalars(select(URL)))
        if not urls:
            return 0, 0

        latest_rows = await session.execute(
            select(IndexStatus).join(
                latest_status_subquery,
                and_(
                    IndexStatus.url_id == latest_status_subquery.c.url_id,
                    IndexStatus.checked_at == latest_status_subquery.c.checked_at,
                ),
            )
        )
        latest_by_url_id = {status.url_id: status for status in latest_rows.scalars()}

        updated_count = 0
        unchecked_count = 0
        for url in urls:
            latest_status = latest_by_url_id.get(url.id)
            if latest_status is None:
                has_change = False
                if url.latest_index_status != URLIndexStatus.UNCHECKED:
                    url.latest_index_status = URLIndexStatus.UNCHECKED
                    has_change = True
                if url.last_checked_at is not None:
                    url.last_checked_at = None
                    has_change = True
                if has_change:
                    updated_count += 1
                unchecked_count += 1
                continue

            derived_status = _derived_status(status=latest_status)
            checked_at = latest_status.checked_at
            normalized_checked_at = (
                checked_at.replace(tzinfo=UTC)
                if checked_at.tzinfo is None
                else checked_at.astimezone(UTC)
            )
            if (
                url.latest_index_status != derived_status
                or url.last_checked_at != normalized_checked_at
            ):
                url.latest_index_status = derived_status
                url.last_checked_at = normalized_checked_at
                updated_count += 1

        await session.commit()
        return updated_count, unchecked_count


async def main() -> None:
    settings = get_settings()
    started_at = datetime.now(UTC)
    updated_count, unchecked_count = await _run_backfill()
    duration_ms = round((datetime.now(UTC) - started_at).total_seconds() * 1000, 2)
    print(
        (
            "Backfill completed "
            f"(database={settings.DATABASE_URL!s}, updated={updated_count}, "
            f"unchecked={unchecked_count}, duration_ms={duration_ms})"
        )
    )
    await close_database()


if __name__ == "__main__":
    asyncio.run(main())
