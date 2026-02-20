"""Async sitemap fetching utilities with retry and conditional request support."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final

import httpx

from seo_indexing_tracker.services.sitemap_decompressor import (
    SitemapDecompressionError,
    decompress_gzipped_content,
    is_gzipped_sitemap,
)

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
DEFAULT_MAX_REDIRECTS: Final[int] = 5
DEFAULT_MAX_RETRIES: Final[int] = 3
DEFAULT_BACKOFF_BASE_SECONDS: Final[float] = 0.5
TRANSIENT_HTTP_STATUS_CODES: Final[frozenset[int]] = frozenset(
    {408, 425, 429, 500, 502, 503, 504}
)
PRIMARY_BROWSER_HEADERS: Final[dict[str, str]] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/133.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
ALTERNATE_BROWSER_HEADERS: Final[dict[str, str]] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.3 Safari/605.1.15"
    ),
    "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.8",
}


@dataclass(slots=True, frozen=True)
class SitemapFetchResult:
    """Normalized sitemap fetch response payload."""

    content: bytes | None
    etag: str | None
    last_modified: str | None
    status_code: int
    url: str
    not_modified: bool


class SitemapFetchError(Exception):
    """Base exception for sitemap fetching failures."""


class SitemapFetchTimeoutError(SitemapFetchError):
    """Raised when sitemap fetch times out after retries."""


class SitemapFetchNetworkError(SitemapFetchError):
    """Raised when sitemap fetch fails due to network issues."""


class SitemapFetchHTTPError(SitemapFetchError):
    """Raised when sitemap fetch receives an unrecoverable HTTP status."""

    def __init__(self, url: str, status_code: int) -> None:
        self.url = url
        self.status_code = status_code
        super().__init__(f"Failed to fetch sitemap {url!r}: HTTP {status_code}")


class SitemapFetchDecompressionError(SitemapFetchError):
    """Raised when a fetched sitemap cannot be decompressed."""


def _build_conditional_headers(
    etag: str | None, last_modified: str | None
) -> dict[str, str]:
    headers: dict[str, str] = {}

    if etag:
        headers["If-None-Match"] = etag

    if last_modified:
        headers["If-Modified-Since"] = last_modified

    return headers


def _retry_delay_seconds(attempt_index: int, backoff_base_seconds: float) -> float:
    return float(backoff_base_seconds * (2**attempt_index))


async def _get_sitemap_with_403_retry(
    *,
    client: httpx.AsyncClient,
    url: str,
    conditional_headers: dict[str, str],
) -> httpx.Response:
    primary_headers = {**PRIMARY_BROWSER_HEADERS, **conditional_headers}
    response = await client.get(url, headers=primary_headers)
    if response.status_code != httpx.codes.FORBIDDEN:
        return response

    alternate_headers = {**ALTERNATE_BROWSER_HEADERS, **conditional_headers}
    return await client.get(url, headers=alternate_headers)


async def fetch_sitemap(
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
) -> SitemapFetchResult:
    """Fetch a sitemap URL asynchronously with retries and conditional headers."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")

    if max_redirects < 0:
        raise ValueError("max_redirects must be zero or greater")

    if max_retries < 0:
        raise ValueError("max_retries must be zero or greater")

    if backoff_base_seconds < 0:
        raise ValueError("backoff_base_seconds must be zero or greater")

    headers = _build_conditional_headers(etag=etag, last_modified=last_modified)
    timeout = httpx.Timeout(timeout_seconds)

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        max_redirects=max_redirects,
    ) as client:
        for attempt in range(max_retries + 1):
            try:
                response = await _get_sitemap_with_403_retry(
                    client=client,
                    url=url,
                    conditional_headers=headers,
                )

                if response.status_code == httpx.codes.NOT_MODIFIED:
                    return SitemapFetchResult(
                        content=None,
                        etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"),
                        status_code=response.status_code,
                        url=str(response.url),
                        not_modified=True,
                    )

                response.raise_for_status()

                content = response.content

                if is_gzipped_sitemap(
                    url=str(response.url),
                    content_encoding=response.headers.get("content-encoding"),
                ):
                    try:
                        content = decompress_gzipped_content(content)
                    except SitemapDecompressionError as exc:
                        raise SitemapFetchDecompressionError(
                            f"Failed to decompress sitemap {url!r}: {exc}"
                        ) from exc

                return SitemapFetchResult(
                    content=content,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                    status_code=response.status_code,
                    url=str(response.url),
                    not_modified=False,
                )
            except httpx.TimeoutException as exc:
                if attempt == max_retries:
                    raise SitemapFetchTimeoutError(
                        f"Timed out fetching sitemap {url!r} after {max_retries + 1} attempts"
                    ) from exc
            except httpx.NetworkError as exc:
                if attempt == max_retries:
                    raise SitemapFetchNetworkError(
                        f"Network error fetching sitemap {url!r} after {max_retries + 1} attempts: {exc}"
                    ) from exc
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                is_transient_status = status_code in TRANSIENT_HTTP_STATUS_CODES
                if not is_transient_status or attempt == max_retries:
                    raise SitemapFetchHTTPError(
                        url=url, status_code=status_code
                    ) from exc
            except httpx.HTTPError as exc:
                raise SitemapFetchError(
                    f"HTTP error while fetching sitemap {url!r}: {exc}"
                ) from exc

            await asyncio.sleep(
                _retry_delay_seconds(
                    attempt_index=attempt,
                    backoff_base_seconds=backoff_base_seconds,
                )
            )

    raise SitemapFetchError(f"Unexpected failure while fetching sitemap {url!r}")
