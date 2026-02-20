"""Recursive sitemap index parsing and discovery helpers."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import SplitResult, urlsplit, urlunsplit

from lxml import etree  # type: ignore[import-untyped]

from seo_indexing_tracker.models.sitemap import SitemapType
from seo_indexing_tracker.services.sitemap_fetcher import fetch_sitemap
from seo_indexing_tracker.services.sitemap_type_detector import detect_sitemap_type

DEFAULT_MAX_DEPTH: Final[int] = 5


class SitemapIndexParserError(Exception):
    """Base exception for sitemap index parser failures."""


class SitemapIndexXMLParseError(SitemapIndexParserError):
    """Raised when sitemap index XML cannot be parsed."""


class SitemapIndexRootTypeError(SitemapIndexParserError):
    """Raised when the root sitemap URL is not a sitemap index."""


@dataclass(slots=True, frozen=True)
class SitemapDiscoveryRecord:
    """Metadata for a discovered sitemap URL from a sitemap index."""

    url: str
    depth: int
    source_index_url: str | None
    lastmod: str | None


@dataclass(slots=True, frozen=True)
class SitemapCircularReference:
    """Circular sitemap reference between index resources."""

    source_index_url: str
    target_url: str
    depth: int


@dataclass(slots=True, frozen=True)
class SitemapIndexParseErrorRecord:
    """Non-fatal parsing error captured during recursive discovery."""

    url: str
    message: str


@dataclass(slots=True)
class SitemapIndexParseProgress:
    """Progress counters for recursive sitemap parsing."""

    fetched_sitemaps: int = 0
    parsed_indexes: int = 0
    parsed_urlsets: int = 0
    discovered_sitemaps: int = 0
    queued_sitemaps: int = 0
    processed_sitemaps: int = 0
    circular_references: int = 0
    max_depth_skips: int = 0
    failed_sitemaps: int = 0


@dataclass(slots=True, frozen=True)
class SitemapIndexParseResult:
    """Structured output for recursive sitemap index parsing."""

    root_url: str
    max_depth: int
    all_sitemap_urls: list[str]
    discovered_sitemaps: list[SitemapDiscoveryRecord]
    circular_references: list[SitemapCircularReference]
    errors: list[SitemapIndexParseErrorRecord]
    progress: SitemapIndexParseProgress


@dataclass(slots=True, frozen=True)
class _SitemapQueueItem:
    url: str
    depth: int


@dataclass(slots=True)
class _MutableParseState:
    progress: SitemapIndexParseProgress = field(
        default_factory=SitemapIndexParseProgress
    )
    discovered_records_by_url: dict[str, SitemapDiscoveryRecord] = field(
        default_factory=dict
    )
    circular_references: list[SitemapCircularReference] = field(default_factory=list)
    errors: list[SitemapIndexParseErrorRecord] = field(default_factory=list)


def _canonicalize_url(url: str) -> str:
    normalized = url.strip()
    split_url = urlsplit(normalized)
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

        if child.text is None:
            return None

        if not isinstance(child.text, str):
            return None

        text_value = child.text.strip()
        if not text_value:
            return None

        return text_value

    return None


def _parse_index_entries(
    xml_content: bytes, *, source_url: str
) -> list[SitemapDiscoveryRecord]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)

    try:
        root = etree.fromstring(xml_content, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise SitemapIndexXMLParseError(
            f"Invalid sitemap index XML at {source_url!r}: {exc}"
        ) from exc

    entries: list[SitemapDiscoveryRecord] = []
    for child in root:
        if not isinstance(child.tag, str):
            continue

        if _normalize_tag_name(child.tag) != "sitemap":
            continue

        loc = _extract_child_text(child, "loc")
        if not loc:
            continue

        entries.append(
            SitemapDiscoveryRecord(
                url=loc,
                depth=0,
                source_index_url=source_url,
                lastmod=_extract_child_text(child, "lastmod"),
            )
        )

    return entries


def _record_error(state: _MutableParseState, *, url: str, message: str) -> None:
    state.progress.failed_sitemaps += 1
    state.errors.append(SitemapIndexParseErrorRecord(url=url, message=message))


async def parse_sitemap_index(
    root_url: str,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> SitemapIndexParseResult:
    """Recursively parse a sitemap index and discover nested sitemap URLs."""

    if max_depth < 0:
        raise ValueError("max_depth must be zero or greater")

    normalized_root_url = _canonicalize_url(root_url)
    queue: deque[_SitemapQueueItem] = deque(
        [_SitemapQueueItem(url=normalized_root_url, depth=0)]
    )
    visited_urls: set[str] = {normalized_root_url}
    discovery_order_urls: list[str] = [normalized_root_url]

    state = _MutableParseState()
    state.progress.queued_sitemaps = 1
    state.progress.discovered_sitemaps = 1
    state.discovered_records_by_url[normalized_root_url] = SitemapDiscoveryRecord(
        url=normalized_root_url,
        depth=0,
        source_index_url=None,
        lastmod=None,
    )

    while queue:
        current_item = queue.popleft()
        state.progress.processed_sitemaps += 1

        try:
            fetched = await fetch_sitemap(current_item.url)
        except Exception as exc:  # noqa: BLE001
            _record_error(
                state,
                url=current_item.url,
                message=f"Failed to fetch sitemap: {exc}",
            )
            continue

        state.progress.fetched_sitemaps += 1

        if fetched.content is None:
            _record_error(
                state,
                url=current_item.url,
                message="Fetched sitemap response has no content",
            )
            continue

        try:
            sitemap_type = detect_sitemap_type(fetched.content)
        except Exception as exc:  # noqa: BLE001
            _record_error(
                state,
                url=current_item.url,
                message=f"Failed to detect sitemap type: {exc}",
            )
            continue

        if current_item.depth == 0 and sitemap_type is not SitemapType.INDEX:
            raise SitemapIndexRootTypeError(
                f"Root sitemap {current_item.url!r} is {sitemap_type.value}, expected INDEX"
            )

        if sitemap_type is SitemapType.URLSET:
            state.progress.parsed_urlsets += 1
            continue

        state.progress.parsed_indexes += 1

        try:
            child_entries = _parse_index_entries(
                fetched.content, source_url=current_item.url
            )
        except SitemapIndexXMLParseError as exc:
            _record_error(state, url=current_item.url, message=str(exc))
            continue

        for child_entry in child_entries:
            normalized_child_url = _canonicalize_url(child_entry.url)

            known_record = state.discovered_records_by_url.get(normalized_child_url)
            if known_record is None:
                state.discovered_records_by_url[normalized_child_url] = (
                    SitemapDiscoveryRecord(
                        url=normalized_child_url,
                        depth=current_item.depth + 1,
                        source_index_url=current_item.url,
                        lastmod=child_entry.lastmod,
                    )
                )
                state.progress.discovered_sitemaps += 1
                discovery_order_urls.append(normalized_child_url)

            if normalized_child_url in visited_urls:
                state.progress.circular_references += 1
                state.circular_references.append(
                    SitemapCircularReference(
                        source_index_url=current_item.url,
                        target_url=normalized_child_url,
                        depth=current_item.depth + 1,
                    )
                )
                continue

            next_depth = current_item.depth + 1
            if next_depth > max_depth:
                state.progress.max_depth_skips += 1
                continue

            visited_urls.add(normalized_child_url)
            queue.append(_SitemapQueueItem(url=normalized_child_url, depth=next_depth))
            state.progress.queued_sitemaps += 1

    discovered_sitemaps = [
        state.discovered_records_by_url[url]
        for url in discovery_order_urls
        if url in state.discovered_records_by_url
    ]

    return SitemapIndexParseResult(
        root_url=normalized_root_url,
        max_depth=max_depth,
        all_sitemap_urls=discovery_order_urls,
        discovered_sitemaps=discovered_sitemaps,
        circular_references=state.circular_references,
        errors=state.errors,
        progress=state.progress,
    )


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "SitemapCircularReference",
    "SitemapDiscoveryRecord",
    "SitemapIndexParseErrorRecord",
    "SitemapIndexParseProgress",
    "SitemapIndexParseResult",
    "SitemapIndexParserError",
    "SitemapIndexRootTypeError",
    "SitemapIndexXMLParseError",
    "parse_sitemap_index",
]
