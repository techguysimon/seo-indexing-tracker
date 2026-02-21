"""URL discovery service with sitemap lastmod change detection."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
import ipaddress
import logging
import socket
from typing import Any, cast
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit
from uuid import UUID

from lxml import etree  # type: ignore[import-untyped]
from sqlalchemy import bindparam, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.models.sitemap import Sitemap, SitemapType
from seo_indexing_tracker.models.url import URL
from seo_indexing_tracker.services.sitemap_fetcher import (
    SitemapFetchError,
    SitemapFetchResult,
    fetch_sitemap,
)
from seo_indexing_tracker.services.sitemap_index_parser import DEFAULT_MAX_DEPTH
from seo_indexing_tracker.services.sitemap_type_detector import (
    SitemapTypeDetectionError,
    detect_sitemap_type,
)
from seo_indexing_tracker.services.sitemap_url_parser import (
    SitemapURLXMLParseError,
    parse_sitemap_urls_stream,
)

DEFAULT_BATCH_SIZE = 500
DEFAULT_INDEX_CHILD_MAX_COUNT = 500
DEFAULT_CHILD_FETCH_MAX_REDIRECT_HOPS = 5

SessionScopeFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

logger = logging.getLogger("seo_indexing_tracker.url_discovery")


@dataclass(slots=True, frozen=True)
class URLDiscoveryResult:
    """Structured URL discovery summary."""

    total_discovered: int
    new_count: int
    modified_count: int
    unchanged_count: int


@dataclass(slots=True, frozen=True)
class _SitemapDocument:
    url: str
    depth: int
    content: bytes
    status_code: int
    content_type: str | None


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
        reason: str | None = None,
    ) -> None:
        self.stage = stage
        self.website_id = website_id
        self.sitemap_id = sitemap_id
        self.sitemap_url = sitemap_url
        self.status_code = status_code
        self.content_type = content_type
        self.reason = reason
        reason_suffix = f": {reason}" if reason else ""
        super().__init__(
            f"URL discovery failed at stage={stage} for sitemap {sitemap_id}{reason_suffix}"
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


def _canonicalize_url(url: str) -> str:
    normalized_url = url.strip()
    split_url = urlsplit(normalized_url)
    canonical_parts = SplitResult(
        scheme=split_url.scheme.lower(),
        netloc=split_url.netloc.lower(),
        path=split_url.path,
        query=split_url.query,
        fragment="",
    )
    return urlunsplit(canonical_parts)


def _normalize_tag_name(tag_name: str) -> str:
    if tag_name.startswith("{"):
        _, _, local_name = tag_name.partition("}")
        return local_name.lower()

    _, _, local_name = tag_name.rpartition(":")
    if local_name:
        return local_name.lower()

    return tag_name.lower()


def _extract_child_text(parent: etree._Element, tag_name: str) -> str | None:
    for child in parent:
        if not isinstance(child.tag, str):
            continue

        if _normalize_tag_name(child.tag) != tag_name:
            continue

        if child.text is None or not isinstance(child.text, str):
            return None

        value = cast(str, child.text).strip()
        if not value:
            return None

        return value

    return None


def _parse_sitemap_index_child_urls(
    xml_content: bytes, *, source_url: str
) -> list[str]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)

    try:
        root = etree.fromstring(xml_content, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise SitemapURLXMLParseError(
            f"Invalid sitemap index XML at {source_url!r}: {exc}"
        ) from exc

    child_urls: list[str] = []
    for child in root:
        if not isinstance(child.tag, str):
            continue

        if _normalize_tag_name(child.tag) != "sitemap":
            continue

        loc = _extract_child_text(child, "loc")
        if not loc:
            continue

        child_urls.append(loc)

    return child_urls


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)

    return value.astimezone(UTC)


def _is_disallowed_ip_address(
    ip_address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return (
        ip_address.is_private
        or ip_address.is_loopback
        or ip_address.is_link_local
        or ip_address.is_reserved
        or ip_address.is_multicast
        or ip_address.is_unspecified
    )


async def _resolve_host_ip_addresses(
    host_name: str,
) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    loop = asyncio.get_running_loop()
    try:
        host_addresses = await loop.getaddrinfo(
            host_name,
            None,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise ValueError("host_dns_resolution_failed") from exc

    resolved_ip_addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for _, _, _, _, sockaddr in host_addresses:
        resolved_ip_addresses.add(ipaddress.ip_address(sockaddr[0]))

    if not resolved_ip_addresses:
        raise ValueError("host_dns_resolution_failed")

    return resolved_ip_addresses


async def _validate_child_sitemap_url_for_fetch(child_sitemap_url: str) -> None:
    parsed_child_sitemap_url = urlsplit(child_sitemap_url.strip())
    if parsed_child_sitemap_url.scheme.lower() not in {"http", "https"}:
        raise ValueError("unsupported_scheme")

    host_name = parsed_child_sitemap_url.hostname
    if not host_name:
        raise ValueError("missing_host")

    try:
        resolved_ip_addresses = {ipaddress.ip_address(host_name)}
    except ValueError:
        resolved_ip_addresses = await _resolve_host_ip_addresses(host_name)

    for resolved_ip_address in sorted(resolved_ip_addresses, key=str):
        if _is_disallowed_ip_address(resolved_ip_address):
            raise ValueError(
                f"resolved_to_disallowed_ip:{resolved_ip_address.compressed}"
            )


def _validate_child_fetch_connect_destination(
    *,
    peer_ip_address: str | None,
) -> None:
    if not peer_ip_address:
        raise ValueError("connect_destination_unavailable")

    try:
        parsed_peer_ip_address = ipaddress.ip_address(peer_ip_address)
    except ValueError as exc:
        raise ValueError("connect_destination_unavailable") from exc

    if _is_disallowed_ip_address(parsed_peer_ip_address):
        raise ValueError(
            f"connect_destination_disallowed:{parsed_peer_ip_address.compressed}"
        )


class URLDiscoveryService:
    """Discover sitemap URLs and classify changes from lastmod metadata."""

    def __init__(
        self,
        *,
        session_factory: SessionScopeFactory | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        index_max_depth: int = DEFAULT_MAX_DEPTH,
        index_child_max_count: int = DEFAULT_INDEX_CHILD_MAX_COUNT,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")

        if index_max_depth < 0:
            raise ValueError("index_max_depth must be zero or greater")

        if index_child_max_count <= 0:
            raise ValueError("index_child_max_count must be greater than zero")

        if session_factory is None:
            from seo_indexing_tracker.database import session_scope

            session_factory = session_scope

        self._session_factory = session_factory
        self._batch_size = batch_size
        self._index_max_depth = index_max_depth
        self._index_child_max_count = index_child_max_count

    async def _fetch_child_sitemap_with_policy(
        self,
        *,
        root_sitemap: Sitemap,
        child_sitemap_url: str,
        max_redirect_hops: int = DEFAULT_CHILD_FETCH_MAX_REDIRECT_HOPS,
    ) -> SitemapFetchResult:
        if max_redirect_hops < 0:
            raise ValueError("max_redirect_hops must be zero or greater")

        current_child_url = child_sitemap_url
        redirect_hops = 0

        while True:
            try:
                await _validate_child_sitemap_url_for_fetch(current_child_url)
            except ValueError as exc:
                raise URLDiscoveryProcessingError(
                    stage="fetch_child_policy",
                    website_id=root_sitemap.website_id,
                    sitemap_id=root_sitemap.id,
                    sitemap_url=child_sitemap_url,
                    reason=str(exc),
                ) from exc

            try:
                fetch_result = await fetch_sitemap(
                    current_child_url,
                    follow_redirects=False,
                )
            except SitemapFetchError as exc:
                raise URLDiscoveryProcessingError(
                    stage="fetch_child",
                    website_id=root_sitemap.website_id,
                    sitemap_id=root_sitemap.id,
                    sitemap_url=child_sitemap_url,
                    status_code=getattr(exc, "status_code", None),
                    content_type=getattr(exc, "content_type", None),
                    reason=exc.__class__.__name__,
                ) from exc

            try:
                _validate_child_fetch_connect_destination(
                    peer_ip_address=fetch_result.peer_ip_address,
                )
            except ValueError as exc:
                raise URLDiscoveryProcessingError(
                    stage="fetch_child_policy",
                    website_id=root_sitemap.website_id,
                    sitemap_id=root_sitemap.id,
                    sitemap_url=child_sitemap_url,
                    status_code=fetch_result.status_code,
                    content_type=fetch_result.content_type,
                    reason=str(exc),
                ) from exc

            if fetch_result.status_code < 300 or fetch_result.status_code >= 400:
                return fetch_result

            if redirect_hops >= max_redirect_hops:
                raise URLDiscoveryProcessingError(
                    stage="fetch_child_policy",
                    website_id=root_sitemap.website_id,
                    sitemap_id=root_sitemap.id,
                    sitemap_url=child_sitemap_url,
                    status_code=fetch_result.status_code,
                    content_type=fetch_result.content_type,
                    reason="redirect_hops_exceeded",
                )

            redirect_location = fetch_result.redirect_location
            if not redirect_location:
                raise URLDiscoveryProcessingError(
                    stage="fetch_child_policy",
                    website_id=root_sitemap.website_id,
                    sitemap_id=root_sitemap.id,
                    sitemap_url=child_sitemap_url,
                    status_code=fetch_result.status_code,
                    content_type=fetch_result.content_type,
                    reason="redirect_missing_location",
                )

            redirect_target_url = urljoin(fetch_result.url, redirect_location)
            try:
                await _validate_child_sitemap_url_for_fetch(redirect_target_url)
            except ValueError as exc:
                raise URLDiscoveryProcessingError(
                    stage="fetch_child_policy",
                    website_id=root_sitemap.website_id,
                    sitemap_id=root_sitemap.id,
                    sitemap_url=child_sitemap_url,
                    status_code=fetch_result.status_code,
                    content_type=fetch_result.content_type,
                    reason=(
                        "redirect_target_disallowed:"
                        f"{_sanitize_sitemap_url(redirect_target_url)}:{exc}"
                    ),
                ) from exc

            current_child_url = redirect_target_url
            redirect_hops += 1

    async def _discover_records_by_url(
        self,
        *,
        root_sitemap: Sitemap,
        root_fetch_result: SitemapFetchResult,
    ) -> dict[str, tuple[datetime | None, str | None, float | None]]:
        if root_fetch_result.content is None:
            raise RuntimeError("Sitemap response content was empty")

        records_by_url: dict[str, tuple[datetime | None, str | None, float | None]] = {}
        queue: deque[_SitemapDocument] = deque(
            [
                _SitemapDocument(
                    url=root_sitemap.url,
                    depth=0,
                    content=root_fetch_result.content,
                    status_code=root_fetch_result.status_code,
                    content_type=root_fetch_result.content_type,
                )
            ]
        )
        discovered_child_sitemap_count = 0
        seen_sitemap_urls: set[str] = {_canonicalize_url(root_sitemap.url)}

        while queue:
            sitemap_document = queue.popleft()

            try:
                sitemap_type = detect_sitemap_type(sitemap_document.content)
            except SitemapTypeDetectionError as exc:
                raise URLDiscoveryProcessingError(
                    stage="parse",
                    website_id=root_sitemap.website_id,
                    sitemap_id=root_sitemap.id,
                    sitemap_url=sitemap_document.url,
                    status_code=sitemap_document.status_code,
                    content_type=sitemap_document.content_type,
                ) from exc

            if sitemap_type is SitemapType.URLSET:
                try:
                    for record in parse_sitemap_urls_stream(sitemap_document.content):
                        records_by_url[record.url] = (
                            _parse_lastmod(record.lastmod),
                            record.changefreq,
                            record.priority,
                        )
                except SitemapURLXMLParseError as exc:
                    raise URLDiscoveryProcessingError(
                        stage="parse",
                        website_id=root_sitemap.website_id,
                        sitemap_id=root_sitemap.id,
                        sitemap_url=sitemap_document.url,
                        status_code=sitemap_document.status_code,
                        content_type=sitemap_document.content_type,
                    ) from exc

                continue

            try:
                child_sitemap_urls = _parse_sitemap_index_child_urls(
                    sitemap_document.content,
                    source_url=sitemap_document.url,
                )
            except SitemapURLXMLParseError as exc:
                raise URLDiscoveryProcessingError(
                    stage="parse",
                    website_id=root_sitemap.website_id,
                    sitemap_id=root_sitemap.id,
                    sitemap_url=sitemap_document.url,
                    status_code=sitemap_document.status_code,
                    content_type=sitemap_document.content_type,
                ) from exc

            next_depth = sitemap_document.depth + 1
            new_child_sitemap_urls: list[str] = []
            for child_sitemap_url in child_sitemap_urls:
                canonical_child_sitemap_url = _canonicalize_url(child_sitemap_url)
                if canonical_child_sitemap_url in seen_sitemap_urls:
                    continue
                new_child_sitemap_urls.append(child_sitemap_url)
                seen_sitemap_urls.add(canonical_child_sitemap_url)

            if new_child_sitemap_urls and next_depth > self._index_max_depth:
                raise URLDiscoveryProcessingError(
                    stage="index_depth_limit",
                    website_id=root_sitemap.website_id,
                    sitemap_id=root_sitemap.id,
                    sitemap_url=sitemap_document.url,
                    status_code=sitemap_document.status_code,
                    content_type=sitemap_document.content_type,
                )

            for child_sitemap_url in new_child_sitemap_urls:
                discovered_child_sitemap_count += 1
                if discovered_child_sitemap_count > self._index_child_max_count:
                    raise URLDiscoveryProcessingError(
                        stage="index_child_limit",
                        website_id=root_sitemap.website_id,
                        sitemap_id=root_sitemap.id,
                        sitemap_url=sitemap_document.url,
                        status_code=sitemap_document.status_code,
                        content_type=sitemap_document.content_type,
                    )

                child_fetch_result = await self._fetch_child_sitemap_with_policy(
                    root_sitemap=root_sitemap,
                    child_sitemap_url=child_sitemap_url,
                )

                if child_fetch_result.content is None:
                    raise URLDiscoveryProcessingError(
                        stage="fetch_child_content",
                        website_id=root_sitemap.website_id,
                        sitemap_id=root_sitemap.id,
                        sitemap_url=child_fetch_result.url,
                        status_code=child_fetch_result.status_code,
                        content_type=child_fetch_result.content_type,
                    )

                queue.append(
                    _SitemapDocument(
                        url=child_fetch_result.url,
                        depth=next_depth,
                        content=child_fetch_result.content,
                        status_code=child_fetch_result.status_code,
                        content_type=child_fetch_result.content_type,
                    )
                )

        return records_by_url

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

            try:
                records_by_url = await self._discover_records_by_url(
                    root_sitemap=sitemap,
                    root_fetch_result=fetch_result,
                )
            except URLDiscoveryProcessingError as exc:
                logger.warning(
                    {
                        "event": "url_discovery_failed",
                        "website_id": str(sitemap.website_id),
                        "sitemap_id": str(sitemap.id),
                        "sitemap_url_sanitized": _sanitize_sitemap_url(exc.sitemap_url),
                        "stage": exc.stage,
                        "exception_class": exc.__class__.__name__,
                        "http_status": exc.status_code,
                        "content_type": exc.content_type,
                        "reason": exc.reason,
                    }
                )
                raise

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
