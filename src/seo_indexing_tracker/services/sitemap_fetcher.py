"""Async sitemap fetching utilities with retry and conditional request support."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import logging
from typing import Any, Final, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx

from seo_indexing_tracker.config import get_settings
from seo_indexing_tracker.services.sitemap_decompressor import (
    SitemapDecompressionError,
    decompress_gzipped_content,
    has_gzip_magic_bytes,
    is_probably_xml_content,
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
    "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
ALTERNATE_BROWSER_HEADERS: Final[dict[str, str]] = {
    "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.8",
}

_logger = logging.getLogger("seo_indexing_tracker.sitemap.fetcher")


@dataclass(slots=True, frozen=True)
class SitemapFetchResult:
    """Normalized sitemap fetch response payload."""

    content: bytes | None
    etag: str | None
    last_modified: str | None
    status_code: int
    content_type: str | None
    url: str
    not_modified: bool
    redirect_location: str | None = None
    peer_ip_address: str | None = None


class SitemapFetchError(Exception):
    """Base exception for sitemap fetching failures."""


class SitemapFetchTimeoutError(SitemapFetchError):
    """Raised when sitemap fetch times out after retries."""


class SitemapFetchNetworkError(SitemapFetchError):
    """Raised when sitemap fetch fails due to network issues."""


class SitemapFetchHTTPError(SitemapFetchError):
    """Raised when sitemap fetch receives an unrecoverable HTTP status."""

    def __init__(
        self,
        url: str,
        status_code: int,
        *,
        content_type: str | None = None,
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.content_type = content_type
        super().__init__(f"Failed to fetch sitemap {url!r}: HTTP {status_code}")


class SitemapFetchDecompressionError(SitemapFetchError):
    """Raised when a fetched sitemap cannot be decompressed."""


@dataclass(slots=True, frozen=True)
class _PinnedRequestSettings:
    request_url: str
    host_header: str
    request_extensions: dict[str, object]


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


def _sanitize_sitemap_url(url: str) -> str:
    split_url = urlsplit(url)
    host = split_url.netloc.rsplit("@", maxsplit=1)[-1]
    path = split_url.path or "/"
    sanitized = f"{host}{path}".strip()
    return sanitized or "sitemap"


def _is_ip_literal(host_name: str) -> bool:
    try:
        ipaddress.ip_address(host_name)
    except ValueError:
        return False
    return True


def _format_host_component(host_name: str) -> str:
    if ":" in host_name and not host_name.startswith("["):
        return f"[{host_name}]"
    return host_name


def _default_port_for_scheme(scheme: str) -> int | None:
    if scheme == "http":
        return 80
    if scheme == "https":
        return 443
    return None


def _build_host_header(*, scheme: str, host_name: str, port: int | None) -> str:
    formatted_host = _format_host_component(host_name)
    default_port = _default_port_for_scheme(scheme)
    if port is None or port == default_port:
        return formatted_host
    return f"{formatted_host}:{port}"


def _replace_url_host(split_url: SplitResult, *, host_name: str) -> str:
    formatted_host = _format_host_component(host_name)
    user_info = ""
    if split_url.username is not None:
        user_info = split_url.username
        if split_url.password is not None:
            user_info = f"{user_info}:{split_url.password}"

    netloc = formatted_host
    if split_url.port is not None:
        netloc = f"{netloc}:{split_url.port}"
    if user_info:
        netloc = f"{user_info}@{netloc}"

    return urlunsplit(
        (
            split_url.scheme,
            netloc,
            split_url.path,
            split_url.query,
            split_url.fragment,
        )
    )


def _build_pinned_request_settings(
    *,
    url: str,
    pinned_connect_ip: str,
) -> _PinnedRequestSettings:
    parsed_url = urlsplit(url)
    scheme = parsed_url.scheme.lower()
    host_name = parsed_url.hostname
    if host_name is None:
        raise ValueError("pinned_request_missing_host")

    try:
        parsed_pinned_connect_ip = ipaddress.ip_address(pinned_connect_ip)
    except ValueError as exc:
        raise ValueError("pinned_request_invalid_ip") from exc

    request_extensions: dict[str, object] = {}
    if scheme == "https" and not _is_ip_literal(host_name):
        request_extensions["sni_hostname"] = host_name

    return _PinnedRequestSettings(
        request_url=_replace_url_host(
            parsed_url,
            host_name=parsed_pinned_connect_ip.compressed,
        ),
        host_header=_build_host_header(
            scheme=scheme,
            host_name=host_name,
            port=parsed_url.port,
        ),
        request_extensions=request_extensions,
    )


def _extract_ip_address_from_peer_address(peer_address: object) -> str | None:
    if isinstance(peer_address, tuple):
        if not peer_address:
            return None
        candidate_host = peer_address[0]
    elif isinstance(peer_address, str):
        candidate_host = peer_address
    else:
        return None

    if not isinstance(candidate_host, str):
        return None

    try:
        return ipaddress.ip_address(candidate_host).compressed
    except ValueError:
        return None


def _extract_peer_ip_address(response: httpx.Response) -> str | None:
    network_stream = response.extensions.get("network_stream")
    if network_stream is None:
        return None

    extra_info_getter = getattr(network_stream, "get_extra_info", None)
    if not callable(extra_info_getter):
        return None

    peer_address_candidates: list[object | None] = []
    for key in ("server_addr", "peername"):
        try:
            peer_address_candidates.append(extra_info_getter(key))
        except Exception:
            peer_address_candidates.append(None)

    try:
        socket_object = extra_info_getter("socket")
    except Exception:
        socket_object = None

    if socket_object is not None and hasattr(socket_object, "getpeername"):
        socket_with_peer = cast(Any, socket_object)
        try:
            peer_address_candidates.append(socket_with_peer.getpeername())
        except OSError:
            peer_address_candidates.append(None)

    for peer_address in peer_address_candidates:
        peer_ip_address = _extract_ip_address_from_peer_address(peer_address)
        if peer_ip_address is not None:
            return peer_ip_address

    return None


async def _get_sitemap_with_403_retry(
    *,
    client: httpx.AsyncClient,
    url: str,
    conditional_headers: dict[str, str],
    user_agent: str,
    host_header: str | None = None,
    request_extensions: dict[str, object] | None = None,
) -> httpx.Response:
    primary_headers = {
        "User-Agent": user_agent,
        **PRIMARY_BROWSER_HEADERS,
        **conditional_headers,
    }
    if host_header is not None:
        primary_headers["Host"] = host_header

    response = await client.get(
        url,
        headers=primary_headers,
        extensions=request_extensions,
    )
    if response.status_code != httpx.codes.FORBIDDEN:
        return response

    alternate_headers = {
        "User-Agent": user_agent,
        **ALTERNATE_BROWSER_HEADERS,
        **conditional_headers,
    }
    if host_header is not None:
        alternate_headers["Host"] = host_header

    return await client.get(
        url,
        headers=alternate_headers,
        extensions=request_extensions,
    )


async def fetch_sitemap(
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    user_agent: str | None = None,
    follow_redirects: bool = True,
    pinned_connect_ip: str | None = None,
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
    request_user_agent = user_agent or get_settings().OUTBOUND_HTTP_USER_AGENT
    pinned_request_settings = (
        _build_pinned_request_settings(
            url=url,
            pinned_connect_ip=pinned_connect_ip,
        )
        if pinned_connect_ip is not None
        else None
    )
    request_url = (
        pinned_request_settings.request_url
        if pinned_request_settings is not None
        else url
    )

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=follow_redirects,
        max_redirects=max_redirects,
    ) as client:
        for attempt in range(max_retries + 1):
            try:
                response = await _get_sitemap_with_403_retry(
                    client=client,
                    url=request_url,
                    conditional_headers=headers,
                    user_agent=request_user_agent,
                    host_header=(
                        pinned_request_settings.host_header
                        if pinned_request_settings is not None
                        else None
                    ),
                    request_extensions=(
                        pinned_request_settings.request_extensions
                        if pinned_request_settings is not None
                        else None
                    ),
                )

                response_url = (
                    str(response.url) if pinned_request_settings is None else url
                )

                if response.status_code == httpx.codes.NOT_MODIFIED:
                    return SitemapFetchResult(
                        content=None,
                        etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"),
                        status_code=response.status_code,
                        content_type=response.headers.get("content-type"),
                        url=response_url,
                        not_modified=True,
                        redirect_location=response.headers.get("location"),
                        peer_ip_address=_extract_peer_ip_address(response),
                    )

                response.raise_for_status()

                content = response.content

                if is_gzipped_sitemap(
                    url=str(response.url),
                    content_encoding=response.headers.get("content-encoding"),
                ):
                    if has_gzip_magic_bytes(content):
                        try:
                            content = decompress_gzipped_content(content)
                        except SitemapDecompressionError as exc:
                            raise SitemapFetchDecompressionError(
                                f"Failed to decompress sitemap {url!r}: {exc}"
                            ) from exc
                    elif not is_probably_xml_content(content):
                        raise SitemapFetchDecompressionError(
                            f"Failed to decompress sitemap {url!r}: "
                            "Response indicated gzip compression, but payload "
                            "was not gzip and did not look like XML"
                        )

                return SitemapFetchResult(
                    content=content,
                    etag=response.headers.get("etag"),
                    last_modified=response.headers.get("last-modified"),
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type"),
                    url=response_url,
                    not_modified=False,
                    redirect_location=response.headers.get("location"),
                    peer_ip_address=_extract_peer_ip_address(response),
                )
            except httpx.TimeoutException as exc:
                _logger.warning(
                    {
                        "event": "sitemap_fetch_timeout",
                        "stage": "fetch",
                        "sitemap_url_sanitized": _sanitize_sitemap_url(url),
                        "attempt": attempt + 1,
                        "max_attempts": max_retries + 1,
                        "exception_class": exc.__class__.__name__,
                    }
                )
                if attempt == max_retries:
                    raise SitemapFetchTimeoutError(
                        f"Timed out fetching sitemap {url!r} after {max_retries + 1} attempts"
                    ) from exc
            except httpx.NetworkError as exc:
                _logger.warning(
                    {
                        "event": "sitemap_fetch_network_error",
                        "stage": "fetch",
                        "sitemap_url_sanitized": _sanitize_sitemap_url(url),
                        "attempt": attempt + 1,
                        "max_attempts": max_retries + 1,
                        "exception_class": exc.__class__.__name__,
                    }
                )
                if attempt == max_retries:
                    raise SitemapFetchNetworkError(
                        f"Network error fetching sitemap {url!r} after {max_retries + 1} attempts: {exc}"
                    ) from exc
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                is_transient_status = status_code in TRANSIENT_HTTP_STATUS_CODES
                _logger.warning(
                    {
                        "event": "sitemap_fetch_http_status",
                        "stage": "fetch",
                        "sitemap_url_sanitized": _sanitize_sitemap_url(url),
                        "attempt": attempt + 1,
                        "max_attempts": max_retries + 1,
                        "http_status": status_code,
                        "content_type": exc.response.headers.get("content-type"),
                        "exception_class": exc.__class__.__name__,
                        "retryable": is_transient_status,
                    }
                )
                if not is_transient_status or attempt == max_retries:
                    raise SitemapFetchHTTPError(
                        url=str(exc.response.url),
                        status_code=status_code,
                        content_type=exc.response.headers.get("content-type"),
                    ) from exc
            except httpx.HTTPError as exc:
                _logger.error(
                    {
                        "event": "sitemap_fetch_http_error",
                        "stage": "fetch",
                        "sitemap_url_sanitized": _sanitize_sitemap_url(url),
                        "attempt": attempt + 1,
                        "max_attempts": max_retries + 1,
                        "exception_class": exc.__class__.__name__,
                    }
                )
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
