"""Tests for sitemap HTTP fetching behavior."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from seo_indexing_tracker.services.sitemap_fetcher import fetch_sitemap


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
    ) -> httpx.Response:
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
