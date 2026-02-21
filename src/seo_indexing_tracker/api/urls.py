"""Website URL listing, filtering, and export API routes."""

from __future__ import annotations

import csv
from datetime import datetime
from io import StringIO
from math import ceil
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.models import (
    IndexStatus,
    IndexVerdict,
    Sitemap,
    URL,
    URLIndexStatus,
    Website,
)

router = APIRouter(prefix="/api/websites/{website_id}/urls", tags=["urls"])


class WebsiteURLListItem(BaseModel):
    """Website URL listing record with latest inspection summary."""

    url: str
    latest_index_status: URLIndexStatus
    last_checked_at: datetime | None
    sitemap_id: UUID | None
    verdict: IndexVerdict | None
    coverage_state: str | None
    last_crawl_time: datetime | None
    google_canonical: str | None
    user_canonical: str | None


class WebsiteURLListResponse(BaseModel):
    """Paginated website URL listing payload."""

    page: int
    page_size: int
    total_items: int
    total_pages: int
    items: list[WebsiteURLListItem]


async def ensure_website_exists(*, session: AsyncSession, website_id: UUID) -> None:
    website = await session.scalar(select(Website.id).where(Website.id == website_id))
    if website is not None:
        return

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Website not found",
    )


async def list_website_sitemaps(
    *,
    session: AsyncSession,
    website_id: UUID,
) -> list[Sitemap]:
    rows = await session.scalars(
        select(Sitemap)
        .where(Sitemap.website_id == website_id)
        .order_by(Sitemap.created_at.desc())
    )
    return list(rows)


def _filters_statement(
    *,
    website_id: UUID,
    status_filter: URLIndexStatus | None,
    sitemap_id: UUID | None,
    search: str,
) -> Select[tuple[UUID]]:
    statement = select(URL.id).where(URL.website_id == website_id)
    if status_filter is not None:
        statement = statement.where(URL.latest_index_status == status_filter)
    if sitemap_id is not None:
        statement = statement.where(URL.sitemap_id == sitemap_id)

    stripped_search = search.strip()
    if stripped_search:
        statement = statement.where(URL.url.ilike(f"%{stripped_search}%"))
    return statement


async def fetch_website_urls(
    *,
    session: AsyncSession,
    website_id: UUID,
    status_filter: URLIndexStatus | None,
    sitemap_id: UUID | None,
    search: str,
    page: int,
    page_size: int,
    include_all: bool,
) -> WebsiteURLListResponse:
    filtered_url_ids = _filters_statement(
        website_id=website_id,
        status_filter=status_filter,
        sitemap_id=sitemap_id,
        search=search,
    ).subquery()

    total_items = int(
        (
            await session.scalar(
                select(func.count()).select_from(filtered_url_ids),
            )
        )
        or 0
    )
    safe_page_size = max(1, min(page_size, 200))
    total_pages = max(1, ceil(total_items / safe_page_size))
    safe_page = max(1, min(page, total_pages))

    latest_status_subquery = (
        select(
            IndexStatus.url_id.label("url_id"),
            func.max(IndexStatus.checked_at).label("checked_at"),
        )
        .group_by(IndexStatus.url_id)
        .subquery()
    )

    listing_statement = (
        select(
            URL.url,
            URL.latest_index_status,
            URL.last_checked_at,
            URL.sitemap_id,
            IndexStatus.verdict,
            IndexStatus.coverage_state,
            IndexStatus.last_crawl_time,
            IndexStatus.google_canonical,
            IndexStatus.user_canonical,
        )
        .where(URL.id.in_(select(filtered_url_ids.c.id)))
        .outerjoin(latest_status_subquery, latest_status_subquery.c.url_id == URL.id)
        .outerjoin(
            IndexStatus,
            and_(
                IndexStatus.url_id == latest_status_subquery.c.url_id,
                IndexStatus.checked_at == latest_status_subquery.c.checked_at,
            ),
        )
        .order_by(URL.updated_at.desc(), URL.url.asc())
    )

    if not include_all:
        listing_statement = listing_statement.offset(
            (safe_page - 1) * safe_page_size
        ).limit(safe_page_size)

    rows = await session.execute(listing_statement)
    items = [
        WebsiteURLListItem(
            url=row.url,
            latest_index_status=row.latest_index_status,
            last_checked_at=row.last_checked_at,
            sitemap_id=row.sitemap_id,
            verdict=row.verdict,
            coverage_state=row.coverage_state,
            last_crawl_time=row.last_crawl_time,
            google_canonical=row.google_canonical,
            user_canonical=row.user_canonical,
        )
        for row in rows
    ]

    response_page = 1 if include_all else safe_page
    response_page_size = total_items if include_all else safe_page_size
    response_total_pages = 1 if include_all else total_pages

    return WebsiteURLListResponse(
        page=response_page,
        page_size=response_page_size,
        total_items=total_items,
        total_pages=response_total_pages,
        items=items,
    )


@router.get("", response_model=WebsiteURLListResponse, status_code=status.HTTP_200_OK)
async def list_website_urls(
    website_id: UUID,
    status_filter: URLIndexStatus | None = Query(default=None, alias="status"),
    sitemap_id: UUID | None = Query(default=None),
    search: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
) -> WebsiteURLListResponse:
    await ensure_website_exists(session=session, website_id=website_id)
    return await fetch_website_urls(
        session=session,
        website_id=website_id,
        status_filter=status_filter,
        sitemap_id=sitemap_id,
        search=search,
        page=page,
        page_size=page_size,
        include_all=False,
    )


@router.get("/export", status_code=status.HTTP_200_OK)
async def export_website_urls(
    website_id: UUID,
    status_filter: URLIndexStatus | None = Query(default=None, alias="status"),
    sitemap_id: UUID | None = Query(default=None),
    search: str = Query(default=""),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await ensure_website_exists(session=session, website_id=website_id)
    payload = await fetch_website_urls(
        session=session,
        website_id=website_id,
        status_filter=status_filter,
        sitemap_id=sitemap_id,
        search=search,
        page=1,
        page_size=200,
        include_all=True,
    )

    csv_buffer = StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(
        [
            "url",
            "latest_index_status",
            "last_checked_at",
            "sitemap_id",
            "verdict",
            "coverage_state",
            "last_crawl_time",
            "google_canonical",
            "user_canonical",
            "canonical_mismatch",
        ]
    )
    for item in payload.items:
        canonical_mismatch = bool(
            item.google_canonical
            and item.user_canonical
            and item.google_canonical != item.user_canonical
        )
        writer.writerow(
            [
                item.url,
                item.latest_index_status.value,
                item.last_checked_at.isoformat() if item.last_checked_at else "",
                str(item.sitemap_id) if item.sitemap_id else "",
                item.verdict.value if item.verdict else "",
                item.coverage_state or "",
                item.last_crawl_time.isoformat() if item.last_crawl_time else "",
                item.google_canonical or "",
                item.user_canonical or "",
                "true" if canonical_mismatch else "false",
            ]
        )

    csv_body = csv_buffer.getvalue()
    return Response(
        content=csv_body,
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="website-{website_id}-urls-export.csv"'
            )
        },
    )
