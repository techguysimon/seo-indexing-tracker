"""Google Search Console URL Inspection API client with async wrappers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
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

WEBMASTERS_SCOPE = "https://www.googleapis.com/auth/webmasters"
_LOGGER = logging.getLogger("seo_indexing_tracker.google_api.search_console")


class InspectionSystemStatus(str, Enum):
    """Normalized system status derived from Search Console coverage state."""

    INDEXED = "INDEXED"
    NOT_INDEXED = "NOT_INDEXED"
    BLOCKED = "BLOCKED"
    SOFT_404 = "SOFT_404"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True, frozen=True)
class IndexStatusResult:
    """Parsed URL inspection result with normalized status and error details."""

    inspection_url: str
    site_url: str
    success: bool
    http_status: int | None
    system_status: InspectionSystemStatus
    verdict: str | None
    coverage_state: str | None
    last_crawl_time: datetime | None
    indexing_state: str | None
    robots_txt_state: str | None
    raw_response: dict[str, Any] | None
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


def _is_valid_url(url: str) -> bool:
    parsed_url = urlparse(url)
    return parsed_url.scheme in {"http", "https"} and bool(parsed_url.netloc)


def _is_valid_site_url(site_url: str) -> bool:
    normalized_site_url = site_url.strip()
    if normalized_site_url.startswith("sc-domain:"):
        return normalized_site_url != "sc-domain:"

    return _is_valid_url(normalized_site_url)


def _error_code_for_google_api_error(error: GoogleAPIError) -> str:
    if isinstance(error, QuotaExceededError):
        return "QUOTA_EXCEEDED"
    if isinstance(error, AuthenticationError):
        return "AUTH_ERROR"
    if isinstance(error, InvalidURLError):
        return "INVALID_REQUEST"
    return "API_ERROR"


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None

    if not isinstance(value, str):
        raise ValueError("lastCrawlTime must be an ISO datetime string")

    normalized_value = value.strip()
    if normalized_value == "":
        return None

    if normalized_value.endswith("Z"):
        normalized_value = f"{normalized_value[:-1]}+00:00"

    parsed_datetime = datetime.fromisoformat(normalized_value)
    if parsed_datetime.tzinfo is None:
        return parsed_datetime.replace(tzinfo=UTC)

    return parsed_datetime


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str):
        stripped_value = value.strip()
        return stripped_value if stripped_value != "" else None

    return None


def _system_status_from_coverage(coverage_state: str | None) -> InspectionSystemStatus:
    if coverage_state is None:
        return InspectionSystemStatus.UNKNOWN

    normalized_coverage = coverage_state.lower()

    if any(
        keyword in normalized_coverage
        for keyword in ("blocked", "robots.txt", "access denied")
    ):
        return InspectionSystemStatus.BLOCKED

    if "soft 404" in normalized_coverage:
        return InspectionSystemStatus.SOFT_404

    if any(keyword in normalized_coverage for keyword in ("not indexed", "excluded")):
        return InspectionSystemStatus.NOT_INDEXED

    if any(
        keyword in normalized_coverage
        for keyword in (
            "not found (404)",
            "crawled - currently not indexed",
            "discovered - currently not indexed",
            "duplicate, google chose different canonical",
        )
    ):
        return InspectionSystemStatus.NOT_INDEXED

    if any(keyword in normalized_coverage for keyword in ("server error", "error")):
        return InspectionSystemStatus.ERROR

    if "indexed" in normalized_coverage:
        return InspectionSystemStatus.INDEXED

    return InspectionSystemStatus.UNKNOWN


class GoogleURLInspectionClient:
    """Search Console URL Inspection API client with asyncio wrappers."""

    def __init__(
        self,
        *,
        credentials_path: str | Path,
        scopes: list[str] | tuple[str, ...] | None = None,
        builder: _GoogleBuildCallable = build,
    ) -> None:
        base_scopes = [WEBMASTERS_SCOPE]
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
            "searchconsole",
            "v1",
            credentials=credentials,
            cache_discovery=False,
        )

    @property
    def _search_console_service(self) -> Any:
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _error_result(
        self,
        *,
        inspection_url: str,
        site_url: str,
        http_status: int | None,
        error_code: str,
        error_message: str,
        raw_response: dict[str, Any] | None = None,
        retry_after_seconds: int | None = None,
    ) -> IndexStatusResult:
        return IndexStatusResult(
            inspection_url=inspection_url,
            site_url=site_url,
            success=False,
            http_status=http_status,
            system_status=InspectionSystemStatus.UNKNOWN,
            verdict=None,
            coverage_state=None,
            last_crawl_time=None,
            indexing_state=None,
            robots_txt_state=None,
            raw_response=raw_response,
            error_code=error_code,
            error_message=error_message,
            retry_after_seconds=retry_after_seconds,
        )

    def inspect_url_sync(self, url: str, site_url: str) -> IndexStatusResult:
        """Inspect a URL and return parsed index status details."""

        normalized_url = url.strip()
        normalized_site_url = site_url.strip()

        if not _is_valid_url(normalized_url):
            _LOGGER.warning(
                "google_api_invalid_url",
                extra={
                    "service": "searchconsole",
                    "operation": "urlInspection.index.inspect",
                    "inspection_url": url,
                    "site_url": site_url,
                },
            )
            return self._error_result(
                inspection_url=url,
                site_url=site_url,
                http_status=None,
                error_code="INVALID_URL",
                error_message="inspection URL must include an http or https scheme and hostname",
            )

        if not _is_valid_site_url(normalized_site_url):
            _LOGGER.warning(
                "google_api_invalid_url",
                extra={
                    "service": "searchconsole",
                    "operation": "urlInspection.index.inspect",
                    "inspection_url": normalized_url,
                    "site_url": site_url,
                },
            )
            return self._error_result(
                inspection_url=normalized_url,
                site_url=site_url,
                http_status=None,
                error_code="INVALID_SITE_URL",
                error_message=(
                    "site_url must be an http/https URL-prefix property or sc-domain:<domain>"
                ),
            )

        try:
            response = execute_with_google_retry(
                lambda: cast(
                    dict[str, Any],
                    self._search_console_service.urlInspection()
                    .index()
                    .inspect(
                        body={
                            "inspectionUrl": normalized_url,
                            "siteUrl": normalized_site_url,
                        }
                    )
                    .execute(),
                ),
                operation="urlInspection.index.inspect",
                service="searchconsole",
            )
            inspection_result = cast(
                dict[str, Any], response.get("inspectionResult", {})
            )
            index_status_result = cast(
                dict[str, Any], inspection_result.get("indexStatusResult", {})
            )

            coverage_state = _optional_text(index_status_result.get("coverageState"))
            return IndexStatusResult(
                inspection_url=normalized_url,
                site_url=normalized_site_url,
                success=True,
                http_status=200,
                system_status=_system_status_from_coverage(coverage_state),
                verdict=_optional_text(index_status_result.get("verdict")),
                coverage_state=coverage_state,
                last_crawl_time=_parse_datetime(
                    index_status_result.get("lastCrawlTime")
                ),
                indexing_state=_optional_text(index_status_result.get("indexingState")),
                robots_txt_state=_optional_text(
                    index_status_result.get("robotsTxtState")
                ),
                raw_response=response,
                error_code=None,
                error_message=None,
                retry_after_seconds=None,
            )
        except ValueError as error:
            return self._error_result(
                inspection_url=normalized_url,
                site_url=normalized_site_url,
                http_status=200,
                error_code="PARSE_ERROR",
                error_message=str(error),
            )
        except GoogleAPIError as error:
            return self._error_result(
                inspection_url=normalized_url,
                site_url=normalized_site_url,
                http_status=error.status_code,
                error_code=_error_code_for_google_api_error(error),
                error_message=error.message,
                retry_after_seconds=error.retry_after_seconds,
            )
        except GoogleCredentialsError as error:
            _LOGGER.error(
                "google_api_credentials_error",
                extra={
                    "service": "searchconsole",
                    "operation": "credentials.load",
                    "credentials_path": self._credentials_path,
                },
            )
            return self._error_result(
                inspection_url=normalized_url,
                site_url=normalized_site_url,
                http_status=None,
                error_code="AUTH_ERROR",
                error_message=str(error),
            )

    async def inspect_url(
        self,
        url: str,
        site_url: str,
        *,
        website_id: UUID | None = None,
        session: AsyncSession | None = None,
        quota_discovery_service: QuotaDiscoveryService | None = None,
    ) -> IndexStatusResult:
        """Async wrapper for URL inspection."""

        result = await asyncio.to_thread(self.inspect_url_sync, url, site_url)
        if website_id is None or session is None:
            return result

        discovery_service = quota_discovery_service or QuotaDiscoveryService()
        if result.http_status == 429:
            await discovery_service.record_429(
                session=session,
                website_id=website_id,
                api_type="inspection",
                retry_after_seconds=result.retry_after_seconds,
            )
            return result
        if result.success:
            await discovery_service.record_success(
                session=session,
                website_id=website_id,
                api_type="inspection",
            )
        return result


__all__ = [
    "GoogleURLInspectionClient",
    "IndexStatusResult",
    "InspectionSystemStatus",
]
