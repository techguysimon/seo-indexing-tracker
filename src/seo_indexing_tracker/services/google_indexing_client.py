"""Google Indexing API v3 client with sync and async wrappers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlparse
from uuid import UUID

from googleapiclient.discovery import build  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.services.google_credentials import (
    GoogleCredentialsError,
    load_service_account_credentials,
)
from seo_indexing_tracker.services.google_errors import (
    AuthenticationError,
    GoogleAPIError,
    InvalidURLError,
    QuotaExceededError,
    execute_with_google_retry,
)
from seo_indexing_tracker.services.quota_discovery_service import QuotaDiscoveryService

INDEXING_SCOPE = "https://www.googleapis.com/auth/indexing"
ALLOWED_NOTIFICATION_ACTIONS = frozenset({"URL_UPDATED", "URL_DELETED"})
MAX_BATCH_SUBMIT_SIZE = 100
_LOGGER = logging.getLogger("seo_indexing_tracker.google_api.indexing")


@dataclass(slots=True, frozen=True)
class IndexingURLResult:
    """Single URL submission status."""

    url: str
    action: str
    success: bool
    http_status: int | None
    metadata: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    retry_after_seconds: int | None


@dataclass(slots=True, frozen=True)
class BatchSubmitResult:
    """Batch submission status with per-URL results."""

    action: str
    total_urls: int
    success_count: int
    failure_count: int
    results: list[IndexingURLResult]


@dataclass(slots=True, frozen=True)
class MetadataLookupResult:
    """Result for URL metadata lookup."""

    url: str
    success: bool
    http_status: int | None
    metadata: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    retry_after_seconds: int | None


class _GoogleBuildCallable(Protocol):
    def __call__(
        self,
        service_name: str,
        version: str,
        *,
        credentials: Any,
        cache_discovery: bool,
    ) -> Any: ...


def _normalize_action(action: str) -> str:
    normalized_action = action.strip().upper()
    if normalized_action in ALLOWED_NOTIFICATION_ACTIONS:
        return normalized_action

    allowed_actions_text = ", ".join(sorted(ALLOWED_NOTIFICATION_ACTIONS))
    raise ValueError(f"action must be one of: {allowed_actions_text}")


def _is_valid_url(url: str) -> bool:
    parsed_url = urlparse(url)
    return parsed_url.scheme in {"http", "https"} and bool(parsed_url.netloc)


def _error_code_for_google_api_error(error: GoogleAPIError) -> str:
    if isinstance(error, QuotaExceededError):
        return "QUOTA_EXCEEDED"
    if isinstance(error, AuthenticationError):
        return "AUTH_ERROR"
    if isinstance(error, InvalidURLError):
        return "INVALID_URL"
    return "API_ERROR"


class GoogleIndexingClient:
    """Google Indexing API client with asyncio wrappers around sync calls."""

    def __init__(
        self,
        *,
        credentials_path: str | Path,
        scopes: list[str] | tuple[str, ...] | None = None,
        builder: _GoogleBuildCallable = build,
    ) -> None:
        base_scopes = [INDEXING_SCOPE]
        if scopes is not None:
            base_scopes.extend(scopes)

        deduplicated_scopes = list(dict.fromkeys(base_scopes))
        self._credentials_path = str(Path(credentials_path).expanduser().resolve())
        self._scopes = deduplicated_scopes
        self._builder = builder
        self._service: Any | None = None

    def _build_service(self) -> Any:
        credentials = load_service_account_credentials(
            self._credentials_path,
            scopes=self._scopes,
        )
        return self._builder(
            "indexing",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

    @property
    def _indexing_service(self) -> Any:
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _submission_error_result(
        self,
        *,
        url: str,
        action: str,
        http_status: int | None,
        error_code: str,
        error_message: str,
        retry_after_seconds: int | None = None,
    ) -> IndexingURLResult:
        return IndexingURLResult(
            url=url,
            action=action,
            success=False,
            http_status=http_status,
            metadata=None,
            error_code=error_code,
            error_message=error_message,
            retry_after_seconds=retry_after_seconds,
        )

    def submit_url_sync(
        self,
        url: str,
        action: str = "URL_UPDATED",
    ) -> IndexingURLResult:
        """Submit a single URL notification to the Google Indexing API."""

        normalized_action = _normalize_action(action)
        if not _is_valid_url(url):
            validation_error = InvalidURLError(
                "URL must include an http or https scheme and hostname",
                status_code=None,
                reason=None,
                details=None,
                operation="urlNotifications.publish",
                service="indexing",
                retry_after_seconds=None,
            )
            _LOGGER.warning(
                "google_api_invalid_url",
                extra={
                    "service": "indexing",
                    "operation": "urlNotifications.publish",
                    "url": url,
                    "action": normalized_action,
                },
            )
            return self._submission_error_result(
                url=url,
                action=normalized_action,
                http_status=None,
                error_code="INVALID_URL",
                error_message=validation_error.message,
            )

        try:
            response = execute_with_google_retry(
                lambda: cast(
                    dict[str, Any],
                    self._indexing_service.urlNotifications()
                    .publish(body={"url": url, "type": normalized_action})
                    .execute(),
                ),
                operation="urlNotifications.publish",
                service="indexing",
            )
            metadata = cast(dict[str, Any], response.get("urlNotificationMetadata", {}))
            return IndexingURLResult(
                url=url,
                action=normalized_action,
                success=True,
                http_status=200,
                metadata=metadata,
                error_code=None,
                error_message=None,
                retry_after_seconds=None,
            )
        except GoogleAPIError as error:
            return self._submission_error_result(
                url=url,
                action=normalized_action,
                http_status=error.status_code,
                error_code=_error_code_for_google_api_error(error),
                error_message=error.message,
                retry_after_seconds=error.retry_after_seconds,
            )
        except GoogleCredentialsError as error:
            _LOGGER.error(
                "google_api_credentials_error",
                extra={
                    "service": "indexing",
                    "operation": "credentials.load",
                    "credentials_path": self._credentials_path,
                },
            )
            return self._submission_error_result(
                url=url,
                action=normalized_action,
                http_status=None,
                error_code="AUTH_ERROR",
                error_message=str(error),
                retry_after_seconds=None,
            )

    async def submit_url(
        self,
        url: str,
        action: str = "URL_UPDATED",
        *,
        website_id: UUID | None = None,
        session: AsyncSession | None = None,
        quota_discovery_service: QuotaDiscoveryService | None = None,
    ) -> IndexingURLResult:
        """Async wrapper for submitting a single URL."""

        result = await asyncio.to_thread(self.submit_url_sync, url, action)
        await self._record_quota_observation(
            result=result,
            website_id=website_id,
            session=session,
            quota_discovery_service=quota_discovery_service,
        )
        return result

    def batch_submit_sync(
        self,
        urls: list[str] | tuple[str, ...],
        action: str = "URL_UPDATED",
    ) -> BatchSubmitResult:
        """Submit up to 100 URLs and return per-URL statuses."""

        if len(urls) > MAX_BATCH_SUBMIT_SIZE:
            raise ValueError(
                f"batch_submit accepts at most {MAX_BATCH_SUBMIT_SIZE} URLs"
            )

        normalized_action = _normalize_action(action)
        results = [
            self.submit_url_sync(url=url, action=normalized_action)
            for url in list(urls)
        ]
        success_count = sum(1 for result in results if result.success)
        failure_count = len(results) - success_count
        return BatchSubmitResult(
            action=normalized_action,
            total_urls=len(results),
            success_count=success_count,
            failure_count=failure_count,
            results=results,
        )

    async def batch_submit(
        self,
        urls: list[str] | tuple[str, ...],
        action: str = "URL_UPDATED",
        *,
        website_id: UUID | None = None,
        session: AsyncSession | None = None,
        quota_discovery_service: QuotaDiscoveryService | None = None,
    ) -> BatchSubmitResult:
        """Async wrapper for batch URL submission."""

        batch_result = await asyncio.to_thread(self.batch_submit_sync, urls, action)
        for result in batch_result.results:
            await self._record_quota_observation(
                result=result,
                website_id=website_id,
                session=session,
                quota_discovery_service=quota_discovery_service,
            )
        return batch_result

    def get_metadata_sync(self, url: str) -> MetadataLookupResult:
        """Fetch URL notification metadata from the Google Indexing API."""

        if not _is_valid_url(url):
            validation_error = InvalidURLError(
                "URL must include an http or https scheme and hostname",
                status_code=None,
                reason=None,
                details=None,
                operation="urlNotifications.getMetadata",
                service="indexing",
                retry_after_seconds=None,
            )
            _LOGGER.warning(
                "google_api_invalid_url",
                extra={
                    "service": "indexing",
                    "operation": "urlNotifications.getMetadata",
                    "url": url,
                },
            )
            return MetadataLookupResult(
                url=url,
                success=False,
                http_status=None,
                metadata=None,
                error_code="INVALID_URL",
                error_message=validation_error.message,
                retry_after_seconds=None,
            )

        try:
            response = execute_with_google_retry(
                lambda: cast(
                    dict[str, Any],
                    self._indexing_service.urlNotifications()
                    .getMetadata(url=url)
                    .execute(),
                ),
                operation="urlNotifications.getMetadata",
                service="indexing",
            )
            return MetadataLookupResult(
                url=url,
                success=True,
                http_status=200,
                metadata=response,
                error_code=None,
                error_message=None,
                retry_after_seconds=None,
            )
        except GoogleAPIError as error:
            return MetadataLookupResult(
                url=url,
                success=False,
                http_status=error.status_code,
                metadata=None,
                error_code=_error_code_for_google_api_error(error),
                error_message=error.message,
                retry_after_seconds=error.retry_after_seconds,
            )
        except GoogleCredentialsError as error:
            _LOGGER.error(
                "google_api_credentials_error",
                extra={
                    "service": "indexing",
                    "operation": "credentials.load",
                    "credentials_path": self._credentials_path,
                },
            )
            return MetadataLookupResult(
                url=url,
                success=False,
                http_status=None,
                metadata=None,
                error_code="AUTH_ERROR",
                error_message=str(error),
                retry_after_seconds=None,
            )

    async def get_metadata(
        self,
        url: str,
        *,
        website_id: UUID | None = None,
        session: AsyncSession | None = None,
        quota_discovery_service: QuotaDiscoveryService | None = None,
    ) -> MetadataLookupResult:
        """Async wrapper for URL notification metadata lookup."""

        result = await asyncio.to_thread(self.get_metadata_sync, url)
        await self._record_quota_observation(
            result=result,
            website_id=website_id,
            session=session,
            quota_discovery_service=quota_discovery_service,
        )
        return result

    async def _record_quota_observation(
        self,
        *,
        result: IndexingURLResult | MetadataLookupResult,
        website_id: UUID | None,
        session: AsyncSession | None,
        quota_discovery_service: QuotaDiscoveryService | None,
    ) -> None:
        if website_id is None or session is None:
            return

        discovery_service = quota_discovery_service or QuotaDiscoveryService()
        if result.http_status == 429:
            await discovery_service.record_429(
                session=session,
                website_id=website_id,
                api_type="indexing",
                retry_after_seconds=result.retry_after_seconds,
            )
            return
        if result.success:
            await discovery_service.record_success(
                session=session,
                website_id=website_id,
                api_type="indexing",
            )


__all__ = [
    "BatchSubmitResult",
    "GoogleIndexingClient",
    "IndexingURLResult",
    "MetadataLookupResult",
    "MAX_BATCH_SUBMIT_SIZE",
]
