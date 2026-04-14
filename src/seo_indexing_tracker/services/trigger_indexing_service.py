"""Trigger indexing service for URL discovery and queueing."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.models import Sitemap, URL
from seo_indexing_tracker.services.priority_queue import PriorityQueueService
from seo_indexing_tracker.services.sitemap_fetcher import (
    SitemapFetchError,
    SitemapFetchHTTPError,
    SitemapFetchNetworkError,
    SitemapFetchTimeoutError,
)
from seo_indexing_tracker.services.url_discovery import (
    URLDiscoveryProcessingError,
    URLDiscoveryService,
)


@dataclass(slots=True)
class TriggerIndexingResult:
    """Structured result from trigger indexing operation."""

    success: bool
    sitemap_count: int
    discovered_urls: int
    queued_urls: int
    feedback: str | None = None


class SitemapFetchException(Exception):
    """Raised when sitemap fetch fails during trigger indexing.

    This exception wraps the original SitemapFetchError and preserves
    its attributes for error feedback generation.
    """

    def __init__(
        self,
        url: str,
        status_code: int | None = None,
        content_type: str | None = None,
        original_error: SitemapFetchError | None = None,
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.content_type = content_type
        self.original_error = original_error
        super().__init__(
            f"Failed to fetch sitemap {url!r}: HTTP {status_code}"
            if status_code
            else f"Failed to fetch sitemap {url!r}"
        )


class URLDiscoveryProcessingException(URLDiscoveryProcessingError):
    """Raised when URL discovery processing fails during trigger indexing."""

    def __init__(
        self,
        stage: str,
        website_id: UUID,
        sitemap_id: UUID,
        sitemap_url: str,
        status_code: int | None = None,
        content_type: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(
            stage=stage,
            website_id=website_id,
            sitemap_id=sitemap_id,
            sitemap_url=sitemap_url,
            status_code=status_code,
            content_type=content_type,
            reason=reason,
        )


class EnqueueException(Exception):
    """Raised when enqueueing URLs fails during trigger indexing."""

    pass


def _safe_sitemap_url_for_feedback(url: str | None) -> str:
    """Sanitize sitemap URL for safe display in feedback messages."""
    if not url:
        return "sitemap"

    from urllib.parse import urlsplit

    split_url = urlsplit(url)
    host = split_url.netloc.rsplit("@", maxsplit=1)[-1]
    path = split_url.path or "/"
    safe_url = f"{host}{path}".strip()
    return safe_url or "sitemap"


def _trigger_feedback_for_fetch_error(
    error: SitemapFetchError,
    safe_sitemap_url: str,
) -> str:
    """Generate user-friendly feedback for sitemap fetch errors."""
    if isinstance(error, SitemapFetchTimeoutError):
        return (
            "Trigger indexing failed: network timeout while fetching sitemap "
            f"({safe_sitemap_url}). Retry in a moment."
        )

    if isinstance(error, SitemapFetchNetworkError):
        return (
            "Trigger indexing failed: network error while fetching sitemap "
            f"({safe_sitemap_url}). Verify DNS/firewall access and retry."
        )

    if isinstance(error, SitemapFetchHTTPError):
        if error.status_code in {401, 403}:
            return (
                "Trigger indexing failed: sitemap access blocked "
                f"({safe_sitemap_url}, HTTP {error.status_code}). "
                "Verify sitemap access rules and retry."
            )

        return (
            "Trigger indexing failed: sitemap fetch returned an HTTP error "
            f"({safe_sitemap_url}, HTTP {error.status_code})."
        )

    return (
        "Trigger indexing failed: unable to fetch sitemap "
        f"({safe_sitemap_url}). Verify sitemap access rules and retry."
    )


def _trigger_feedback_for_discovery_error(
    error: URLDiscoveryProcessingError,
    safe_sitemap_url: str,
) -> str:
    """Generate user-friendly feedback for URL discovery errors."""
    if error.stage == "parse":
        return (
            "Trigger indexing failed: sitemap response was not valid XML "
            f"({safe_sitemap_url})."
        )

    return (
        "Trigger indexing failed: sitemap discovery failed "
        f"({safe_sitemap_url}) before URLs could be queued."
    )


class TriggerIndexingService:
    """Service for triggering URL discovery and queueing for a website."""

    def __init__(
        self,
        session: AsyncSession,
        discovery_service: URLDiscoveryService | None = None,
        queue_service: PriorityQueueService | None = None,
    ) -> None:
        self._session = session
        self._discovery_service = discovery_service
        self._queue_service = queue_service

    async def trigger_indexing(self, website_id: UUID) -> TriggerIndexingResult:
        """Discover URLs from active sitemaps and enqueue them for processing.

        Args:
            website_id: UUID of the website to trigger indexing for.

        Returns:
            TriggerIndexingResult with counts and status.

        Raises:
            SitemapFetchException: When sitemap fetch fails.
            URLDiscoveryProcessingException: When URL discovery processing fails.
            EnqueueException: When enqueueing URLs fails.
        """
        discovery_service = self._discovery_service or URLDiscoveryService(
            session_factory=self._use_existing_session
        )
        queue_service = self._queue_service or PriorityQueueService(
            session_factory=self._use_existing_session
        )

        sitemap_rows = (
            await self._session.execute(
                select(Sitemap.id, Sitemap.url).where(
                    Sitemap.website_id == website_id,
                    Sitemap.is_active.is_(True),
                )
            )
        ).all()
        sitemap_ids = [row.id for row in sitemap_rows]
        sitemap_urls_by_id = {row.id: row.url for row in sitemap_rows}

        discovered_urls = 0
        for sitemap_id in sitemap_ids:
            sitemap_url = sitemap_urls_by_id.get(sitemap_id)
            try:
                discovery_result = await discovery_service.discover_urls(sitemap_id)
                discovered_urls += (
                    discovery_result.new_count + discovery_result.modified_count
                )
            except SitemapFetchTimeoutError as error:
                error_url = getattr(error, "url", None)
                safe_error_url = (
                    _safe_sitemap_url_for_feedback(error_url)
                    if error_url
                    else _safe_sitemap_url_for_feedback(sitemap_url)
                )
                raise SitemapFetchException(
                    url=safe_error_url,
                    status_code=getattr(error, "status_code", None),
                    content_type=getattr(error, "content_type", None),
                    original_error=error,
                ) from error
            except SitemapFetchNetworkError as error:
                error_url = getattr(error, "url", None)
                safe_error_url = (
                    _safe_sitemap_url_for_feedback(error_url)
                    if error_url
                    else _safe_sitemap_url_for_feedback(sitemap_url)
                )
                raise SitemapFetchException(
                    url=safe_error_url,
                    status_code=getattr(error, "status_code", None),
                    content_type=getattr(error, "content_type", None),
                    original_error=error,
                ) from error
            except SitemapFetchHTTPError as error:
                error_url = getattr(error, "url", None)
                safe_error_url = (
                    _safe_sitemap_url_for_feedback(error_url)
                    if error_url
                    else _safe_sitemap_url_for_feedback(sitemap_url)
                )
                raise SitemapFetchException(
                    url=safe_error_url,
                    status_code=getattr(error, "status_code", None),
                    content_type=getattr(error, "content_type", None),
                    original_error=error,
                ) from error
            except SitemapFetchError as error:
                error_url = getattr(error, "url", None)
                safe_error_url = (
                    _safe_sitemap_url_for_feedback(error_url)
                    if error_url
                    else _safe_sitemap_url_for_feedback(sitemap_url)
                )
                raise SitemapFetchException(
                    url=safe_error_url,
                    status_code=getattr(error, "status_code", None),
                    content_type=getattr(error, "content_type", None),
                    original_error=error,
                ) from error
            except URLDiscoveryProcessingError as error:
                safe_error_url = _safe_sitemap_url_for_feedback(error.sitemap_url)
                raise URLDiscoveryProcessingException(
                    stage=error.stage,
                    website_id=error.website_id,
                    sitemap_id=error.sitemap_id,
                    sitemap_url=error.sitemap_url,
                    status_code=error.status_code,
                    content_type=error.content_type,
                    reason=error.reason,
                ) from error

        website_url_ids = list(
            await self._session.scalars(
                select(URL.id).where(URL.website_id == website_id)
            )
        )
        try:
            queued_urls = await queue_service.enqueue_many(website_url_ids)
        except Exception as error:
            raise EnqueueException(f"Failed to enqueue URLs: {error}") from error

        return TriggerIndexingResult(
            success=True,
            sitemap_count=len(sitemap_ids),
            discovered_urls=discovered_urls,
            queued_urls=queued_urls,
            feedback=(
                f"Indexing triggered: "
                f"refreshed {len(sitemap_ids)} sitemaps, "
                f"discovered {discovered_urls} URLs, "
                f"queued {queued_urls} URLs"
            ),
        )

    def _use_existing_session(self) -> AbstractAsyncContextManager[AsyncSession]:
        """Context manager that yields the existing session."""
        return _UseExistingSession(self._session)


class _UseExistingSession(AbstractAsyncContextManager[AsyncSession]):
    """Context manager that yields an existing session without managing its lifecycle."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(
        self, exc_type: object, exc_val: object, exc_tb: object
    ) -> None:
        pass
