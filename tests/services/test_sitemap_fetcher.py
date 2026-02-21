"""Tests for sitemap HTTP fetching behavior."""

from __future__ import annotations

import gzip
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from seo_indexing_tracker.services.sitemap_fetcher import (
    SitemapFetchDecompressionError,
    fetch_sitemap,
)


@pytest.mark.asyncio
async def test_fetch_sitemap_retries_forbidden_with_configured_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_headers: list[httpx.Headers] = []
    configured_user_agent = "BlueBeastBuildAgent-Test"

    async def fake_get(
        self: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str],
        extensions: dict[str, object] | None = None,
    ) -> httpx.Response:
        del extensions
        request = httpx.Request("GET", url, headers=headers)
        request_headers.append(request.headers)
        if len(request_headers) == 1:
            return httpx.Response(status_code=403, request=request)
        return httpx.Response(status_code=200, request=request, content=b"<urlset />")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(
        "seo_indexing_tracker.services.sitemap_fetcher.get_settings",
        lambda: SimpleNamespace(OUTBOUND_HTTP_USER_AGENT=configured_user_agent),
    )

    result = await fetch_sitemap(
        "https://example.com/sitemap.xml",
        etag='"etag-123"',
        last_modified="Thu, 01 Jan 1970 00:00:00 GMT",
        max_retries=0,
    )

    assert result.status_code == 200
    assert len(request_headers) == 2
    assert request_headers[0]["user-agent"] == configured_user_agent
    assert request_headers[1]["user-agent"] == configured_user_agent
    assert "accept" in request_headers[0]
    assert "accept-language" in request_headers[0]
    assert request_headers[0]["if-none-match"] == '"etag-123"'
    assert request_headers[0]["if-modified-since"] == "Thu, 01 Jan 1970 00:00:00 GMT"
    assert "accept" in request_headers[1]
    assert "accept-language" in request_headers[1]
    assert request_headers[1]["if-none-match"] == '"etag-123"'
    assert request_headers[1]["if-modified-since"] == "Thu, 01 Jan 1970 00:00:00 GMT"


@pytest.mark.asyncio
async def test_fetch_sitemap_accepts_already_decoded_xml_when_gzip_header_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xml_payload = b"<?xml version='1.0' encoding='UTF-8'?><urlset></urlset>"

    async def fake_get(
        self: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str],
        extensions: dict[str, object] | None = None,
    ) -> httpx.Response:
        del self, headers, extensions
        request = httpx.Request("GET", url)
        response = httpx.Response(
            status_code=200,
            request=request,
            headers={"content-type": "application/xml"},
            content=xml_payload,
        )
        response.headers["content-encoding"] = "gzip"
        return response

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(
        "seo_indexing_tracker.services.sitemap_fetcher.get_settings",
        lambda: SimpleNamespace(OUTBOUND_HTTP_USER_AGENT="TestAgent/1.0"),
    )

    result = await fetch_sitemap("https://example.com/sitemap.xml", max_retries=0)

    assert result.status_code == 200
    assert result.content == xml_payload


@pytest.mark.asyncio
async def test_fetch_sitemap_decompresses_gzip_payload_when_magic_header_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xml_payload = b"<urlset><url><loc>https://example.com/</loc></url></urlset>"
    gzipped_payload = gzip.compress(xml_payload)

    async def fake_get(
        self: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str],
        extensions: dict[str, object] | None = None,
    ) -> httpx.Response:
        del self, headers, extensions
        request = httpx.Request("GET", url)
        return httpx.Response(
            status_code=200,
            request=request,
            headers={"content-encoding": "gzip", "content-type": "application/xml"},
            content=gzipped_payload,
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(
        "seo_indexing_tracker.services.sitemap_fetcher.get_settings",
        lambda: SimpleNamespace(OUTBOUND_HTTP_USER_AGENT="TestAgent/1.0"),
    )

    result = await fetch_sitemap("https://example.com/sitemap.xml", max_retries=0)

    assert result.content == xml_payload


@pytest.mark.asyncio
async def test_fetch_sitemap_raises_for_non_xml_non_gzip_payload_marked_gzip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get(
        self: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str],
        extensions: dict[str, object] | None = None,
    ) -> httpx.Response:
        del self, headers, extensions
        request = httpx.Request("GET", url)
        response = httpx.Response(
            status_code=200,
            request=request,
            headers={"content-type": "application/octet-stream"},
            content=b"\x00\x01\x02\x03",
        )
        response.headers["content-encoding"] = "gzip"
        return response

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(
        "seo_indexing_tracker.services.sitemap_fetcher.get_settings",
        lambda: SimpleNamespace(OUTBOUND_HTTP_USER_AGENT="TestAgent/1.0"),
    )

    with pytest.raises(SitemapFetchDecompressionError):
        await fetch_sitemap("https://example.com/sitemap.xml", max_retries=0)


@pytest.mark.asyncio
async def test_fetch_sitemap_raises_for_corrupted_gzip_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corrupted_gzip_payload = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03bad"

    async def fake_get(
        self: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str],
        extensions: dict[str, object] | None = None,
    ) -> httpx.Response:
        del self, headers, extensions
        request = httpx.Request("GET", url)
        return httpx.Response(
            status_code=200,
            request=request,
            headers={"content-encoding": "gzip", "content-type": "application/xml"},
            content=corrupted_gzip_payload,
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(
        "seo_indexing_tracker.services.sitemap_fetcher.get_settings",
        lambda: SimpleNamespace(OUTBOUND_HTTP_USER_AGENT="TestAgent/1.0"),
    )

    with pytest.raises(SitemapFetchDecompressionError):
        await fetch_sitemap("https://example.com/sitemap.xml", max_retries=0)


@pytest.mark.asyncio
async def test_fetch_sitemap_accepts_xml_for_gz_suffix_when_payload_not_gzipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xml_payload = b"<?xml version='1.0'?><urlset></urlset>"

    async def fake_get(
        self: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str],
        extensions: dict[str, object] | None = None,
    ) -> httpx.Response:
        del self, headers, extensions
        request = httpx.Request("GET", url)
        return httpx.Response(
            status_code=200,
            request=request,
            headers={"content-type": "application/xml"},
            content=xml_payload,
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(
        "seo_indexing_tracker.services.sitemap_fetcher.get_settings",
        lambda: SimpleNamespace(OUTBOUND_HTTP_USER_AGENT="TestAgent/1.0"),
    )

    result = await fetch_sitemap("https://example.com/sitemap.xml.gz", max_retries=0)

    assert result.content == xml_payload


@pytest.mark.asyncio
async def test_fetch_sitemap_extracts_peer_ip_address_from_network_stream_extensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeSocket:
        def getpeername(self) -> tuple[str, int]:
            return ("203.0.113.20", 443)

    class _FakeNetworkStream:
        def get_extra_info(self, key: str) -> Any:
            if key == "server_addr":
                return ("203.0.113.10", 443)
            if key == "peername":
                return ("203.0.113.11", 443)
            if key == "socket":
                return _FakeSocket()
            return None

    async def fake_get(
        self: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str],
        extensions: dict[str, object] | None = None,
    ) -> httpx.Response:
        del self, headers, extensions
        request = httpx.Request("GET", url)
        return httpx.Response(
            status_code=200,
            request=request,
            content=b"<urlset />",
            extensions={"network_stream": _FakeNetworkStream()},
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(
        "seo_indexing_tracker.services.sitemap_fetcher.get_settings",
        lambda: SimpleNamespace(OUTBOUND_HTTP_USER_AGENT="TestAgent/1.0"),
    )

    result = await fetch_sitemap("https://example.com/sitemap.xml", max_retries=0)

    assert result.peer_ip_address == "203.0.113.10"


@pytest.mark.asyncio
async def test_fetch_sitemap_returns_none_when_peer_ip_metadata_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeNetworkStream:
        def get_extra_info(self, key: str) -> None:
            del key
            return None

    async def fake_get(
        self: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str],
        extensions: dict[str, object] | None = None,
    ) -> httpx.Response:
        del self, headers, extensions
        request = httpx.Request("GET", url)
        return httpx.Response(
            status_code=200,
            request=request,
            content=b"<urlset />",
            extensions={"network_stream": _FakeNetworkStream()},
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(
        "seo_indexing_tracker.services.sitemap_fetcher.get_settings",
        lambda: SimpleNamespace(OUTBOUND_HTTP_USER_AGENT="TestAgent/1.0"),
    )

    result = await fetch_sitemap("https://example.com/sitemap.xml", max_retries=0)

    assert result.peer_ip_address is None
