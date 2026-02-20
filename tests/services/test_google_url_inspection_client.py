"""Tests for Search Console URL Inspection API client wrappers and parsing."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

from seo_indexing_tracker.services.google_url_inspection_client import (
    GoogleURLInspectionClient,
    InspectionSystemStatus,
)


class _FakeRequest:
    def __init__(
        self, response: dict[str, Any] | None, error: Exception | None
    ) -> None:
        self._response = response
        self._error = error

    def execute(self) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return {} if self._response is None else self._response


class _FakeInspectionIndex:
    def __init__(self, *, inspection_map: dict[str, Any]) -> None:
        self._inspection_map = inspection_map

    def inspect(self, *, body: dict[str, str]) -> _FakeRequest:
        inspection_url = body["inspectionUrl"]
        payload_or_error = self._inspection_map[inspection_url]
        if isinstance(payload_or_error, Exception):
            return _FakeRequest(None, payload_or_error)
        return _FakeRequest(payload_or_error, None)


class _FakeURLInspectionService:
    def __init__(self, *, inspection_map: dict[str, Any]) -> None:
        self._index = _FakeInspectionIndex(inspection_map=inspection_map)

    def index(self) -> _FakeInspectionIndex:
        return self._index


class _FakeSearchConsoleService:
    def __init__(self, *, inspection_map: dict[str, Any]) -> None:
        self._url_inspection = _FakeURLInspectionService(inspection_map=inspection_map)

    def urlInspection(self) -> _FakeURLInspectionService:  # noqa: N802
        return self._url_inspection


def _http_error(status: int, reason: str, content: str) -> HttpError:
    response = SimpleNamespace(status=status, reason=reason)
    return HttpError(response, content.encode("utf-8"), uri=None)


def test_inspect_url_parses_index_status_and_applies_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_scopes: list[str] = []

    def fake_credentials_loader(
        credentials_path: str,
        *,
        scopes: list[str] | tuple[str, ...] | None = None,
    ) -> object:
        del credentials_path
        captured_scopes.extend(scopes or [])
        return object()

    monkeypatch.setattr(
        "seo_indexing_tracker.services.google_url_inspection_client.load_service_account_credentials",
        fake_credentials_loader,
    )

    service = _FakeSearchConsoleService(
        inspection_map={
            "https://example.com/indexed": {
                "inspectionResult": {
                    "indexStatusResult": {
                        "verdict": "PASS",
                        "coverageState": "Submitted and indexed",
                        "lastCrawlTime": "2026-02-20T08:15:30Z",
                        "indexingState": "INDEXING_ALLOWED",
                        "robotsTxtState": "ALLOWED",
                    }
                }
            }
        }
    )
    client = GoogleURLInspectionClient(
        credentials_path="/tmp/fake-service-account.json",
        builder=lambda *_args, **_kwargs: service,
    )

    result = client.inspect_url_sync(
        "https://example.com/indexed", "sc-domain:example.com"
    )

    assert result.success is True
    assert result.error_code is None
    assert result.verdict == "PASS"
    assert result.coverage_state == "Submitted and indexed"
    assert result.indexing_state == "INDEXING_ALLOWED"
    assert result.robots_txt_state == "ALLOWED"
    assert result.system_status == InspectionSystemStatus.INDEXED
    assert result.last_crawl_time == datetime.fromisoformat("2026-02-20T08:15:30+00:00")
    assert "https://www.googleapis.com/auth/webmasters" in captured_scopes


@pytest.mark.parametrize(
    ("coverage_state", "expected_status"),
    [
        ("Submitted and indexed", InspectionSystemStatus.INDEXED),
        ("Crawled - currently not indexed", InspectionSystemStatus.NOT_INDEXED),
        ("Blocked by robots.txt", InspectionSystemStatus.BLOCKED),
        ("Soft 404", InspectionSystemStatus.SOFT_404),
        ("Server error (5xx)", InspectionSystemStatus.ERROR),
    ],
)
def test_inspect_url_maps_coverage_state_to_system_status(
    monkeypatch: pytest.MonkeyPatch,
    coverage_state: str,
    expected_status: InspectionSystemStatus,
) -> None:
    monkeypatch.setattr(
        "seo_indexing_tracker.services.google_url_inspection_client.load_service_account_credentials",
        lambda *_args, **_kwargs: object(),
    )

    service = _FakeSearchConsoleService(
        inspection_map={
            "https://example.com/page": {
                "inspectionResult": {
                    "indexStatusResult": {
                        "coverageState": coverage_state,
                    }
                }
            }
        }
    )
    client = GoogleURLInspectionClient(
        credentials_path="/tmp/fake-service-account.json",
        builder=lambda *_args, **_kwargs: service,
    )

    result = client.inspect_url_sync("https://example.com/page", "https://example.com/")

    assert result.success is True
    assert result.system_status == expected_status


def test_inspect_url_handles_invalid_request_arguments() -> None:
    client = GoogleURLInspectionClient(
        credentials_path="/tmp/fake-service-account.json",
        builder=lambda *_args, **_kwargs: _FakeSearchConsoleService(inspection_map={}),
    )

    invalid_url_result = client.inspect_url_sync(
        "example.com/no-scheme", "https://example.com/"
    )
    invalid_site_result = client.inspect_url_sync(
        "https://example.com/page", "not-a-site"
    )

    assert invalid_url_result.success is False
    assert invalid_url_result.error_code == "INVALID_URL"
    assert invalid_site_result.success is False
    assert invalid_site_result.error_code == "INVALID_SITE_URL"


def test_inspect_url_maps_quota_and_auth_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "seo_indexing_tracker.services.google_url_inspection_client.load_service_account_credentials",
        lambda *_args, **_kwargs: object(),
    )

    quota_error = _http_error(
        429,
        "Too Many Requests",
        '{"error": {"message": "Quota exceeded"}}',
    )
    auth_error = _http_error(
        403,
        "Forbidden",
        '{"error": {"message": "Insufficient authentication scopes"}}',
    )

    service = _FakeSearchConsoleService(
        inspection_map={
            "https://example.com/quota": quota_error,
            "https://example.com/auth": auth_error,
        }
    )
    client = GoogleURLInspectionClient(
        credentials_path="/tmp/fake-service-account.json",
        builder=lambda *_args, **_kwargs: service,
    )

    quota_result = client.inspect_url_sync(
        "https://example.com/quota", "https://example.com/"
    )
    auth_result = client.inspect_url_sync(
        "https://example.com/auth", "https://example.com/"
    )

    assert quota_result.success is False
    assert quota_result.error_code == "QUOTA_EXCEEDED"
    assert auth_result.success is False
    assert auth_result.error_code == "AUTH_ERROR"


@pytest.mark.asyncio
async def test_inspect_url_async_wrapper_returns_sync_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "seo_indexing_tracker.services.google_url_inspection_client.load_service_account_credentials",
        lambda *_args, **_kwargs: object(),
    )

    service = _FakeSearchConsoleService(
        inspection_map={
            "https://example.com/async": {
                "inspectionResult": {
                    "indexStatusResult": {
                        "coverageState": "Submitted and indexed",
                    }
                }
            }
        }
    )
    client = GoogleURLInspectionClient(
        credentials_path="/tmp/fake-service-account.json",
        builder=lambda *_args, **_kwargs: service,
    )

    result = await client.inspect_url(
        "https://example.com/async", "https://example.com/"
    )

    assert result.success is True
    assert result.system_status == InspectionSystemStatus.INDEXED
