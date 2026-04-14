"""Single URL inspection service for bypassing rate limits."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.models import IndexStatus, ServiceAccount, URL, Website
from seo_indexing_tracker.services.google_api_factory import (
    WebsiteGoogleAPIClients,
    WebsiteServiceAccountConfig,
)
from seo_indexing_tracker.utils.index_status import (
    derive_url_index_status_from_coverage_state,
)
from seo_indexing_tracker.utils.shared_helpers import (
    extract_index_status_result,
    optional_text,
    parse_verdict,
)


@dataclass(slots=True)
class SingleURLInspectionResult:
    """Result of a single URL inspection operation."""

    success: bool
    error_message: str | None = None
    error_type: str | None = None
    item: dict[str, Any] | None = None


async def _build_url_item(
    session: AsyncSession,
    url_id: UUID,
) -> dict[str, Any]:
    """Build a URL item dict matching WebsiteURLListItem structure."""
    latest_status_subquery = (
        select(
            IndexStatus.url_id.label("url_id"),
            func.max(IndexStatus.checked_at).label("checked_at"),
        )
        .where(IndexStatus.url_id == url_id)
        .group_by(IndexStatus.url_id)
        .subquery()
    )

    row = await session.execute(
        select(
            URL.url,
            URL.latest_index_status,
            URL.last_checked_at,
            URL.last_submitted_at,
            URL.sitemap_id,
            IndexStatus.verdict,
            IndexStatus.coverage_state,
            IndexStatus.last_crawl_time,
            IndexStatus.google_canonical,
            IndexStatus.user_canonical,
        )
        .where(URL.id == url_id)
        .outerjoin(latest_status_subquery, latest_status_subquery.c.url_id == URL.id)
        .outerjoin(
            IndexStatus,
            (IndexStatus.url_id == latest_status_subquery.c.url_id)
            & (IndexStatus.checked_at == latest_status_subquery.c.checked_at),
        )
    )
    result = row.first()
    if result is None:
        return {
            "id": url_id,
            "url": "",
            "latest_index_status": "UNCHECKED",
            "last_checked_at": None,
            "last_submitted_at": None,
            "sitemap_id": None,
            "verdict": None,
            "coverage_state": None,
            "last_crawl_time": None,
            "google_canonical": None,
            "user_canonical": None,
        }

    return {
        "id": url_id,
        "url": result.url,
        "latest_index_status": result.latest_index_status,
        "last_checked_at": result.last_checked_at,
        "last_submitted_at": result.last_submitted_at,
        "sitemap_id": result.sitemap_id,
        "verdict": result.verdict,
        "coverage_state": result.coverage_state,
        "last_crawl_time": result.last_crawl_time,
        "google_canonical": result.google_canonical,
        "user_canonical": result.user_canonical,
    }


async def inspect_single_url(
    session: AsyncSession,
    website_id: UUID,
    url_id: UUID,
) -> SingleURLInspectionResult:
    """Inspect a single URL via Google URL Inspection API, bypassing rate limits.

    Args:
        session: Database session
        website_id: UUID of the website
        url_id: UUID of the URL to inspect

    Returns:
        SingleURLInspectionResult with success status and either item data or error info
    """
    url_record = await session.get(URL, url_id)
    if url_record is None or url_record.website_id != website_id:
        return SingleURLInspectionResult(
            success=False,
            error_message="URL not found",
            error_type="not_found",
        )

    website = await session.get(Website, website_id)
    if website is None:
        return SingleURLInspectionResult(
            success=False,
            error_message="Website not found",
            error_type="not_found",
        )

    service_account = await session.scalar(
        select(ServiceAccount).where(ServiceAccount.website_id == website_id)
    )
    if service_account is None:
        item = await _build_url_item(session, url_id)
        return SingleURLInspectionResult(
            success=False,
            error_message="No service account configured",
            error_type="no_service_account",
            item=item,
        )

    try:
        clients = WebsiteGoogleAPIClients(
            config=WebsiteServiceAccountConfig(
                credentials_path=service_account.credentials_path
            )
        )
        result = await asyncio.to_thread(
            clients.search_console.inspect_url_sync,
            url_record.url,
            website.site_url,
        )
    except Exception as error:
        item = await _build_url_item(session, url_id)
        return SingleURLInspectionResult(
            success=False,
            error_message=f"Request failed: {error}",
            error_type="generic_error",
            item=item,
        )

    if result.http_status == 429:
        item = await _build_url_item(session, url_id)
        return SingleURLInspectionResult(
            success=False,
            error_message="Rate limited by Google, please try again later",
            error_type="rate_limited",
            item=item,
        )

    if result.error_code == "AUTH_ERROR":
        item = await _build_url_item(session, url_id)
        return SingleURLInspectionResult(
            success=False,
            error_message="Authentication failed, check service account",
            error_type="auth_error",
            item=item,
        )

    if not result.success:
        item = await _build_url_item(session, url_id)
        return SingleURLInspectionResult(
            success=False,
            error_message=result.error_message or "Inspection failed",
            error_type="generic_error",
            item=item,
        )

    checked_at = datetime.now(UTC)
    raw_response = result.raw_response or {}
    index_status_result = extract_index_status_result(raw_response)

    index_status = IndexStatus(
        url_id=url_id,
        coverage_state=result.coverage_state or "INSPECTION_FAILED",
        verdict=parse_verdict(result.verdict),
        last_crawl_time=result.last_crawl_time,
        indexed_at=result.last_crawl_time,
        checked_at=checked_at,
        robots_txt_state=result.robots_txt_state,
        indexing_state=result.indexing_state,
        page_fetch_state=optional_text(index_status_result.get("pageFetchState")),
        google_canonical=optional_text(index_status_result.get("googleCanonical")),
        user_canonical=optional_text(index_status_result.get("userCanonical")),
        raw_response=raw_response,
    )
    session.add(index_status)

    derived_status = derive_url_index_status_from_coverage_state(
        result.coverage_state or "INSPECTION_FAILED"
    )
    url_record.latest_index_status = derived_status
    url_record.last_checked_at = checked_at
    await session.flush()

    item = await _build_url_item(session, url_id)
    return SingleURLInspectionResult(
        success=True,
        item=item,
    )
