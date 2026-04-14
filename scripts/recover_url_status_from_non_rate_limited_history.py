"""Recover URL denormalized status from latest non-rate-limited inspection history."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import make_url

from seo_indexing_tracker.config import get_settings
from seo_indexing_tracker.database import AsyncSessionFactory, close_database
from seo_indexing_tracker.models import IndexStatus, URL, URLIndexStatus
from seo_indexing_tracker.utils.index_status import (
    derive_url_index_status_from_coverage_state,
)

TARGET_WEBSITE_ID = UUID("84e504dce3ee4c3bb46d2a598fe154d7")
TRANSIENT_ERROR_CODES = {"RATE_LIMITED", "QUOTA_EXCEEDED"}


@dataclass(slots=True, frozen=True)
class _UpdatedURL:
    url: str
    old_status: URLIndexStatus
    new_status: URLIndexStatus
    old_checked_at: datetime | None
    new_checked_at: datetime


def _resolve_sqlite_db_path(database_url: str) -> Path:
    parsed_url = make_url(database_url)
    if parsed_url.get_backend_name() != "sqlite":
        raise RuntimeError("Recovery script supports SQLite databases only")

    database_path = parsed_url.database
    if database_path is None or database_path in {":memory:", ""}:
        raise RuntimeError("Recovery script requires a file-backed SQLite database")

    resolved_path = Path(database_path)
    if not resolved_path.is_absolute():
        resolved_path = Path.cwd() / resolved_path
    return resolved_path


def _backup_sqlite_database(*, source_db_path: Path, backup_db_path: Path) -> None:
    backup_db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source_db_path) as source_connection:
        with sqlite3.connect(backup_db_path) as backup_connection:
            source_connection.backup(backup_connection)


def _status_counts(urls: list[URL]) -> dict[str, int]:
    counts: dict[str, int] = {status.value: 0 for status in URLIndexStatus}
    for url in urls:
        counts[url.latest_index_status.value] += 1
    return counts


def _normalize_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _inspection_error_code(status: IndexStatus) -> str | None:
    raw_response = status.raw_response
    if not isinstance(raw_response, dict):
        return None

    error_code = raw_response.get("error_code")
    if isinstance(error_code, str):
        normalized = error_code.strip().upper()
        if normalized:
            return normalized
    return None


async def _recover() -> tuple[int, dict[str, int], dict[str, int], list[_UpdatedURL]]:
    async with AsyncSessionFactory() as session:
        website_urls = list(
            await session.scalars(
                select(URL)
                .where(URL.website_id == TARGET_WEBSITE_ID)
                .order_by(URL.url.asc())
            )
        )
        if not website_urls:
            return 0, {}, {}, []

        before_counts = _status_counts(website_urls)
        urls_by_id = {url.id: url for url in website_urls}
        history_rows = list(
            await session.scalars(
                select(IndexStatus)
                .join(URL, URL.id == IndexStatus.url_id)
                .where(URL.website_id == TARGET_WEBSITE_ID)
                .order_by(IndexStatus.url_id.asc(), IndexStatus.checked_at.desc())
            )
        )

        latest_qualifying_status_by_url_id: dict[UUID, IndexStatus] = {}
        for row in history_rows:
            if row.url_id in latest_qualifying_status_by_url_id:
                continue
            if _inspection_error_code(row) in TRANSIENT_ERROR_CODES:
                continue
            latest_qualifying_status_by_url_id[row.url_id] = row

        updated_urls: list[_UpdatedURL] = []
        for url_id, qualifying_status in latest_qualifying_status_by_url_id.items():
            url = urls_by_id.get(url_id)
            if url is None:
                continue

            derived_status = derive_url_index_status_from_coverage_state(
                qualifying_status.coverage_state
            )
            normalized_checked_at = _normalize_timestamp(qualifying_status.checked_at)
            if (
                url.latest_index_status == derived_status
                and url.last_checked_at == normalized_checked_at
            ):
                continue

            updated_urls.append(
                _UpdatedURL(
                    url=url.url,
                    old_status=url.latest_index_status,
                    new_status=derived_status,
                    old_checked_at=url.last_checked_at,
                    new_checked_at=normalized_checked_at,
                )
            )
            url.latest_index_status = derived_status
            url.last_checked_at = normalized_checked_at

        await session.commit()

        refreshed_urls = list(
            await session.scalars(
                select(URL)
                .where(URL.website_id == TARGET_WEBSITE_ID)
                .order_by(URL.url.asc())
            )
        )
        after_counts = _status_counts(refreshed_urls)
        return len(updated_urls), before_counts, after_counts, updated_urls


def _format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "None"
    return _normalize_timestamp(value).isoformat()


async def main() -> None:
    settings = get_settings()
    database_path = _resolve_sqlite_db_path(settings.DATABASE_URL)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = Path("data/backups") / (
        f"seo_indexing_tracker_pre_status_recovery_{timestamp}.db"
    )

    _backup_sqlite_database(
        source_db_path=database_path,
        backup_db_path=backup_path,
    )

    updated_count, before_counts, after_counts, updated_urls = await _recover()

    print(f"backup_path={backup_path}")
    print(f"website_id={TARGET_WEBSITE_ID}")
    print(f"updated_urls={updated_count}")
    print("before_counts=" + str(before_counts))
    print("after_counts=" + str(after_counts))

    if updated_urls:
        print("sample_updated_urls=")
        for item in updated_urls[:10]:
            print(
                " - "
                f"url={item.url} "
                f"old_status={item.old_status.value} "
                f"new_status={item.new_status.value} "
                f"old_checked_at={_format_timestamp(item.old_checked_at)} "
                f"new_checked_at={_format_timestamp(item.new_checked_at)}"
            )

    await close_database()


if __name__ == "__main__":
    asyncio.run(main())
