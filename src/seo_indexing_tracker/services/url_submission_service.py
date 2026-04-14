"""Single URL submission service for bypassing rate limits."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.models import ServiceAccount, URL, Website
from seo_indexing_tracker.services.google_api_factory import (
    WebsiteGoogleAPIClients,
    WebsiteServiceAccountConfig,
)
from seo_indexing_tracker.services.url_item_builder import build_url_item


@dataclass(slots=True)
class SingleURLSubmissionResult:
    """Result of a single URL submission operation."""

    success: bool
    error_message: str | None = None
    error_type: str | None = None
    item: dict[str, Any] | None = None


async def submit_single_url(
    session: AsyncSession,
    website_id: UUID,
    url_id: UUID,
) -> SingleURLSubmissionResult:
    """Submit a single URL via Google Indexing API, bypassing rate limits.

    Args:
        session: Database session
        website_id: UUID of the website
        url_id: UUID of the URL to submit

    Returns:
        SingleURLSubmissionResult with success status and either item data or error info
    """
    url_record = await session.get(URL, url_id)
    if url_record is None or url_record.website_id != website_id:
        return SingleURLSubmissionResult(
            success=False,
            error_message="URL not found",
            error_type="not_found",
        )

    website = await session.get(Website, website_id)
    if website is None:
        return SingleURLSubmissionResult(
            success=False,
            error_message="Website not found",
            error_type="not_found",
        )

    service_account = await session.scalar(
        select(ServiceAccount).where(ServiceAccount.website_id == website_id)
    )
    if service_account is None:
        item = await build_url_item(session, url_id)
        return SingleURLSubmissionResult(
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
            clients.indexing.submit_url_sync,
            url_record.url,
            "URL_UPDATED",
        )
    except Exception as error:
        item = await build_url_item(session, url_id)
        return SingleURLSubmissionResult(
            success=False,
            error_message=f"Request failed: {error}",
            error_type="generic_error",
            item=item,
        )

    if result.http_status == 429:
        item = await build_url_item(session, url_id)
        return SingleURLSubmissionResult(
            success=False,
            error_message="Rate limited by Google, please try again later",
            error_type="rate_limited",
            item=item,
        )

    if result.error_code == "AUTH_ERROR":
        item = await build_url_item(session, url_id)
        return SingleURLSubmissionResult(
            success=False,
            error_message="Authentication failed, check service account",
            error_type="auth_error",
            item=item,
        )

    if not result.success:
        item = await build_url_item(session, url_id)
        return SingleURLSubmissionResult(
            success=False,
            error_message=result.error_message or "Submission failed",
            error_type="generic_error",
            item=item,
        )

    url_record.last_submitted_at = datetime.now(UTC)
    await session.flush()

    item = await build_url_item(session, url_id)
    return SingleURLSubmissionResult(
        success=True,
        item=item,
    )
