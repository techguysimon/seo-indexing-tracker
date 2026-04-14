"""Queue page rendering service for the web UI layer."""

from __future__ import annotations

from collections.abc import Iterable
from math import ceil
from typing import TypedDict
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.models import URL, Website


class QueueFilters(TypedDict):
    page: int
    page_size: int
    website_id: UUID | None
    queued_only: bool
    search: str


DEFAULT_PAGE_SIZE = 12
MAX_PAGE_SIZE = 100


def _safe_page_size(value: int) -> int:
    return min(max(value, 1), MAX_PAGE_SIZE)


def _query_filters(
    *,
    page: int,
    page_size: int,
    website_id: UUID | None,
    queued_only: bool,
    search: str,
) -> QueueFilters:
    return {
        "page": max(page, 1),
        "page_size": _safe_page_size(page_size),
        "website_id": website_id,
        "queued_only": queued_only,
        "search": search.strip(),
    }


def _base_queue_statement(*, filters: QueueFilters) -> Select[tuple[URL, str]]:
    statement = select(URL, Website.domain).join(Website, Website.id == URL.website_id)
    website_id = filters["website_id"]
    if website_id is not None:
        statement = statement.where(URL.website_id == website_id)

    if filters["queued_only"]:
        statement = statement.where(URL.current_priority > 0)

    search = filters["search"]
    if search:
        statement = statement.where(URL.url.ilike(f"%{search}%"))

    return statement


async def _fetch_queue_rows(
    *,
    session: AsyncSession,
    filters: QueueFilters,
) -> dict[str, object]:
    page = filters["page"]
    page_size = filters["page_size"]
    base_statement = _base_queue_statement(filters=filters)

    count_statement = select(func.count()).select_from(base_statement.subquery())
    total_items = int((await session.scalar(count_statement)) or 0)
    total_pages = max(1, ceil(total_items / page_size))
    safe_page = min(page, total_pages)

    rows = await session.execute(
        base_statement.order_by(URL.current_priority.desc(), URL.updated_at.desc())
        .offset((safe_page - 1) * page_size)
        .limit(page_size)
    )

    items = [
        {
            "id": row[0].id,
            "website_id": row[0].website_id,
            "website_domain": row[1],
            "url": row[0].url,
            "current_priority": row[0].current_priority,
            "manual_priority_override": row[0].manual_priority_override,
            "updated_at": row[0].updated_at,
        }
        for row in rows.all()
    ]

    return {
        "items": items,
        "page": safe_page,
        "page_size": page_size,
        "total_items": total_items,
        "total_pages": total_pages,
    }


def _table_context(
    *,
    filters: QueueFilters,
    queue_data: dict[str, object],
    websites: Iterable[Website],
    feedback: str | None,
) -> dict[str, object]:
    return {
        "filters": filters,
        "queue_data": queue_data,
        "websites": list(websites),
        "feedback": feedback,
    }
