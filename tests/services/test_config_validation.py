"""Tests for configuration URL validation HTTP behavior."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from seo_indexing_tracker.services.config_validation import (
    ConfigurationValidationService,
)


@pytest.mark.asyncio
async def test_validate_website_url_uses_configured_user_agent_for_head_and_get_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_user_agent = "BlueBeastBuildAgent-Validation"
    requests: list[httpx.Request] = []

    async def fake_request(
        self: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
    ) -> httpx.Response:
        request = httpx.Request(method, url, headers=headers)
        requests.append(request)
        if method == "HEAD":
            return httpx.Response(status_code=403, request=request)
        return httpx.Response(status_code=200, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    monkeypatch.setattr(
        "seo_indexing_tracker.services.config_validation.get_settings",
        lambda: SimpleNamespace(OUTBOUND_HTTP_USER_AGENT=configured_user_agent),
    )

    service = ConfigurationValidationService()
    validated = await service.validate_website_url(site_url="https://example.com")

    assert validated == "https://example.com"
    assert [request.method for request in requests] == ["HEAD", "GET"]
    assert requests[0].headers["user-agent"] == configured_user_agent
    assert requests[1].headers["user-agent"] == configured_user_agent


@pytest.mark.asyncio
async def test_validate_sitemap_url_uses_configured_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_user_agent = "BlueBeastBuildAgent-SitemapValidation"
    requests: list[httpx.Request] = []

    async def fake_request(
        self: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
    ) -> httpx.Response:
        request = httpx.Request(method, url, headers=headers)
        requests.append(request)
        return httpx.Response(status_code=200, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    monkeypatch.setattr(
        "seo_indexing_tracker.services.config_validation.get_settings",
        lambda: SimpleNamespace(OUTBOUND_HTTP_USER_AGENT=configured_user_agent),
    )

    service = ConfigurationValidationService()
    validated = await service.validate_sitemap_url(
        sitemap_url="https://example.com/sitemap.xml"
    )

    assert validated == "https://example.com/sitemap.xml"
    assert len(requests) == 1
    assert requests[0].headers["user-agent"] == configured_user_agent
