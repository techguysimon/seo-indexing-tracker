"""URL discovery service with sitemap lastmod change detection."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
import logging
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import bindparam, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.models.sitemap import Sitemap
from seo_indexing_tracker.models.url import URL
from seo_indexing_tracker.services.sitemap_fetcher import (
    SitemapFetchError,
    fetch_sitemap,
)
from seo_indexing_tracker.services.sitemap_url_parser import (
    SitemapURLXMLParseError,
    parse_sitemap_urls_stream,
)

DEFAULT_BATCH_SIZE = 500

SessionScopeFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

logger = logging.getLogger("seo_indexing_tracker.url_discovery")


@dataclass(slots=True, frozen=True)
class URLDiscoveryResult:
    """Structured URL discovery summary."""

    total_discovered: int
    new_count: int
    modified_count: int
    unchanged_count: int


class URLDiscoveryProcessingError(Exception):
    """Raised when URL discovery fails during parse or persistence."""

    def __init__(
        self,
        *,
        stage: str,
        website_id: UUID,
        sitemap_id: UUID,
        sitemap_url: str,
        status_code: int | None = None,
        content_type: str | None = None,
    ) -> None:
        self.stage = stage
        self.website_id = website_id
        self.sitemap_id = sitemap_id
        self.sitemap_url = sitemap_url
        self.status_code = status_code
        self.content_type = content_type
        super().__init__(
            f"URL discovery failed at stage={stage} for sitemap {sitemap_id}"
        )


def _sanitize_sitemap_url(url: str) -> str:
    split_url = urlsplit(url)
    host = split_url.netloc.rsplit("@", maxsplit=1)[-1]
    path = split_url.path or "/"
    sanitized = f"{host}{path}".strip()
    return sanitized or "sitemap"


def _parse_lastmod(lastmod: str | None) -> datetime | None:
    if lastmod is None:
        return None

    normalized_lastmod = lastmod.strip()
    if not normalized_lastmod:
        return None

    parse_candidate = normalized_lastmod
    if parse_candidate.endswith("Z"):
        parse_candidate = f"{parse_candidate[:-1]}+00:00"

    parsed_datetime: datetime | None = None
    try:
        parsed_datetime = datetime.fromisoformat(parse_candidate)
    except ValueError:
        try:
            parsed_date = date.fromisoformat(normalized_lastmod)
        except ValueError:
            return None
        parsed_datetime = datetime.combine(parsed_date, time.min, tzinfo=UTC)

    if parsed_datetime.tzinfo is None:
        return parsed_datetime.replace(tzinfo=UTC)

    return parsed_datetime.astimezone(UTC)


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)

    return value.astimezone(UTC)


class URLDiscoveryService:
    """Discover sitemap URLs and classify changes from lastmod metadata."""

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

    async def discover_urls(self, sitemap_id: UUID) -> URLDiscoveryResult:
        """Fetch a sitemap, compare URL lastmod values, and persist changes."""

        async with self._session_factory() as session:
            sitemap = await session.get(Sitemap, sitemap_id)
            if sitemap is None:
                raise ValueError(f"Sitemap {sitemap_id} does not exist")

            try:
                fetch_result = await fetch_sitemap(
                    sitemap.url,
                    etag=sitemap.etag,
                    last_modified=sitemap.last_modified_header,
                )
            except SitemapFetchError as exc:
                http_status = getattr(exc, "status_code", None)
                content_type = getattr(exc, "content_type", None)
                logger.warning(
                    {
                        "event": "url_discovery_failed",
                        "website_id": str(sitemap.website_id),
                        "sitemap_id": str(sitemap.id),
                        "sitemap_url_sanitized": _sanitize_sitemap_url(sitemap.url),
                        "stage": "fetch",
                        "exception_class": exc.__class__.__name__,
                        "http_status": http_status,
                        "content_type": content_type,
                    }
                )
                raise

            sitemap.last_fetched = datetime.now(UTC)
            sitemap.etag = fetch_result.etag
            sitemap.last_modified_header = fetch_result.last_modified

            if fetch_result.not_modified:
                return URLDiscoveryResult(
                    total_discovered=0,
                    new_count=0,
                    modified_count=0,
                    unchanged_count=0,
                )

            if fetch_result.content is None:
                raise RuntimeError("Sitemap response content was empty")

            records_by_url: dict[
                str, tuple[datetime | None, str | None, float | None]
            ] = {}
            try:
                for record in parse_sitemap_urls_stream(fetch_result.content):
                    records_by_url[record.url] = (
                        _parse_lastmod(record.lastmod),
                        record.changefreq,
                        record.priority,
                    )
            except SitemapURLXMLParseError as exc:
                logger.warning(
                    {
                        "event": "url_discovery_failed",
                        "website_id": str(sitemap.website_id),
                        "sitemap_id": str(sitemap.id),
                        "sitemap_url_sanitized": _sanitize_sitemap_url(sitemap.url),
                        "stage": "parse",
                        "exception_class": exc.__class__.__name__,
                        "http_status": fetch_result.status_code,
                        "content_type": fetch_result.content_type,
                    }
                )
                raise URLDiscoveryProcessingError(
                    stage="parse",
                    website_id=sitemap.website_id,
                    sitemap_id=sitemap.id,
                    sitemap_url=sitemap.url,
                    status_code=fetch_result.status_code,
                    content_type=fetch_result.content_type,
                ) from exc

            if not records_by_url:
                return URLDiscoveryResult(
                    total_discovered=0,
                    new_count=0,
                    modified_count=0,
                    unchanged_count=0,
                )

            try:
                existing_urls_result = await session.execute(
                    select(URL).where(
                        URL.website_id == sitemap.website_id,
                        URL.url.in_(list(records_by_url.keys())),
                    )
                )
                existing_urls = {
                    row.url: row for row in existing_urls_result.scalars().all()
                }

                new_rows: list[dict[str, object]] = []
                modified_rows: list[dict[str, object]] = []
                unchanged_count = 0

                for discovered_url, (
                    discovered_lastmod,
                    discovered_changefreq,
                    discovered_priority,
                ) in records_by_url.items():
                    existing_url = existing_urls.get(discovered_url)
                    if existing_url is None:
                        new_rows.append(
                            {
                                "website_id": sitemap.website_id,
                                "sitemap_id": sitemap.id,
                                "url": discovered_url,
                                "lastmod": discovered_lastmod,
                                "changefreq": discovered_changefreq,
                                "sitemap_priority": discovered_priority,
                            }
                        )
                        continue

                    normalized_existing_lastmod = _normalize_datetime(
                        existing_url.lastmod
                    )
                    normalized_discovered_lastmod = _normalize_datetime(
                        discovered_lastmod
                    )

                    is_potentially_changed = (
                        normalized_existing_lastmod is None
                        or normalized_discovered_lastmod is None
                    )
                    is_modified = (
                        is_potentially_changed
                        or normalized_existing_lastmod != normalized_discovered_lastmod
                    )

                    if not is_modified:
                        unchanged_count += 1
                        continue

                    modified_rows.append(
                        {
                            "b_id": existing_url.id,
                            "sitemap_id": sitemap.id,
                            "lastmod": discovered_lastmod,
                            "changefreq": discovered_changefreq,
                            "sitemap_priority": discovered_priority,
                        }
                    )

                for start_index in range(0, len(new_rows), self._batch_size):
                    batch = new_rows[start_index : start_index + self._batch_size]
                    await session.execute(insert(URL), batch)

                for start_index in range(0, len(modified_rows), self._batch_size):
                    batch = modified_rows[start_index : start_index + self._batch_size]
                    url_table = cast(Any, URL.__table__)
                    await session.execute(
                        update(url_table)
                        .where(url_table.c.id == bindparam("b_id"))
                        .values(
                            sitemap_id=bindparam("sitemap_id"),
                            lastmod=bindparam("lastmod"),
                            changefreq=bindparam("changefreq"),
                            sitemap_priority=bindparam("sitemap_priority"),
                        )
                        .execution_options(synchronize_session=False),
                        batch,
                    )
            except Exception as exc:
                logger.exception(
                    {
                        "event": "url_discovery_failed",
                        "website_id": str(sitemap.website_id),
                        "sitemap_id": str(sitemap.id),
                        "sitemap_url_sanitized": _sanitize_sitemap_url(sitemap.url),
                        "stage": "discovery",
                        "exception_class": exc.__class__.__name__,
                        "http_status": fetch_result.status_code,
                        "content_type": fetch_result.content_type,
                    }
                )
                raise URLDiscoveryProcessingError(
                    stage="discovery",
                    website_id=sitemap.website_id,
                    sitemap_id=sitemap.id,
                    sitemap_url=sitemap.url,
                    status_code=fetch_result.status_code,
                    content_type=fetch_result.content_type,
                ) from exc

            return URLDiscoveryResult(
                total_discovered=len(records_by_url),
                new_count=len(new_rows),
                modified_count=len(modified_rows),
                unchanged_count=unchanged_count,
            )


__all__ = [
    "URLDiscoveryProcessingError",
    "URLDiscoveryResult",
    "URLDiscoveryService",
]
