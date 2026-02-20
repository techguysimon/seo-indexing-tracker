"""Tests for Google Indexing API v3 client wrappers and error handling."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

from seo_indexing_tracker.services.google_indexing_client import (
    GoogleIndexingClient,
    MAX_BATCH_SUBMIT_SIZE,
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


class _FakeURLNotifications:
    def __init__(
        self, *, publish_map: dict[str, Any], metadata_map: dict[str, Any]
    ) -> None:
        self._publish_map = publish_map
        self._metadata_map = metadata_map

    def publish(self, *, body: dict[str, str]) -> _FakeRequest:
        url = body["url"]
        payload_or_error = self._publish_map[url]
        if isinstance(payload_or_error, Exception):
            return _FakeRequest(None, payload_or_error)
        return _FakeRequest(payload_or_error, None)

    def getMetadata(self, *, url: str) -> _FakeRequest:  # noqa: N802
        payload_or_error = self._metadata_map[url]
        if isinstance(payload_or_error, Exception):
            return _FakeRequest(None, payload_or_error)
        return _FakeRequest(payload_or_error, None)


class _FakeIndexingService:
    def __init__(
        self, *, publish_map: dict[str, Any], metadata_map: dict[str, Any]
    ) -> None:
        self._notifications = _FakeURLNotifications(
            publish_map=publish_map,
            metadata_map=metadata_map,
        )

    def urlNotifications(self) -> _FakeURLNotifications:  # noqa: N802
        return self._notifications


def _http_error(status: int, reason: str, content: str) -> HttpError:
    response = SimpleNamespace(status=status, reason=reason)
    return HttpError(response, content.encode("utf-8"), uri=None)


def test_submit_url_and_metadata_success(monkeypatch: pytest.MonkeyPatch) -> None:
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
        "seo_indexing_tracker.services.google_indexing_client.load_service_account_credentials",
        fake_credentials_loader,
    )

    service = _FakeIndexingService(
        publish_map={
            "https://example.com/page": {
                "urlNotificationMetadata": {
                    "url": "https://example.com/page",
                    "latestUpdate": {"type": "URL_UPDATED"},
                }
            }
        },
        metadata_map={
            "https://example.com/page": {
                "url": "https://example.com/page",
                "latestUpdate": {"type": "URL_UPDATED"},
            }
        },
    )
    client = GoogleIndexingClient(
        credentials_path="/tmp/fake-service-account.json",
        builder=lambda *_args, **_kwargs: service,
    )

    submit_result = client.submit_url_sync("https://example.com/page")
    metadata_result = client.get_metadata_sync("https://example.com/page")

    assert submit_result.success is True
    assert submit_result.error_code is None
    assert metadata_result.success is True
    assert metadata_result.error_code is None
    assert "https://www.googleapis.com/auth/indexing" in captured_scopes


def test_submit_url_returns_invalid_url_error_for_bad_input() -> None:
    client = GoogleIndexingClient(
        credentials_path="/tmp/fake-service-account.json",
        builder=lambda *_args, **_kwargs: _FakeIndexingService(
            publish_map={},
            metadata_map={},
        ),
    )

    result = client.submit_url_sync("example.com/no-scheme")

    assert result.success is False
    assert result.error_code == "INVALID_URL"


def test_submit_url_maps_quota_and_auth_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "seo_indexing_tracker.services.google_indexing_client.load_service_account_credentials",
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

    service = _FakeIndexingService(
        publish_map={
            "https://example.com/quota": quota_error,
            "https://example.com/auth": auth_error,
        },
        metadata_map={},
    )
    client = GoogleIndexingClient(
        credentials_path="/tmp/fake-service-account.json",
        builder=lambda *_args, **_kwargs: service,
    )

    quota_result = client.submit_url_sync("https://example.com/quota")
    auth_result = client.submit_url_sync("https://example.com/auth")

    assert quota_result.success is False
    assert quota_result.error_code == "QUOTA_EXCEEDED"
    assert auth_result.success is False
    assert auth_result.error_code == "AUTH_ERROR"


def test_batch_submit_returns_per_url_statuses_and_enforces_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "seo_indexing_tracker.services.google_indexing_client.load_service_account_credentials",
        lambda *_args, **_kwargs: object(),
    )

    service = _FakeIndexingService(
        publish_map={
            "https://example.com/a": {
                "urlNotificationMetadata": {"url": "https://example.com/a"}
            },
            "https://example.com/b": _http_error(
                429,
                "Too Many Requests",
                '{"error": {"message": "Quota exceeded"}}',
            ),
        },
        metadata_map={},
    )
    client = GoogleIndexingClient(
        credentials_path="/tmp/fake-service-account.json",
        builder=lambda *_args, **_kwargs: service,
    )

    result = client.batch_submit_sync(
        [
            "https://example.com/a",
            "https://example.com/b",
        ]
    )

    assert result.total_urls == 2
    assert result.success_count == 1
    assert result.failure_count == 1
    assert [item.success for item in result.results] == [True, False]

    with pytest.raises(ValueError, match=f"at most {MAX_BATCH_SUBMIT_SIZE} URLs"):
        client.batch_submit_sync(
            [
                f"https://example.com/{index}"
                for index in range(MAX_BATCH_SUBMIT_SIZE + 1)
            ]
        )


@pytest.mark.asyncio
async def test_async_wrappers_return_sync_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "seo_indexing_tracker.services.google_indexing_client.load_service_account_credentials",
        lambda *_args, **_kwargs: object(),
    )

    service = _FakeIndexingService(
        publish_map={
            "https://example.com/async": {
                "urlNotificationMetadata": {"url": "https://example.com/async"}
            }
        },
        metadata_map={
            "https://example.com/async": {
                "url": "https://example.com/async",
                "latestUpdate": {"type": "URL_UPDATED"},
            }
        },
    )
    client = GoogleIndexingClient(
        credentials_path="/tmp/fake-service-account.json",
        builder=lambda *_args, **_kwargs: service,
    )

    submit_result = await client.submit_url("https://example.com/async")
    batch_result = await client.batch_submit(["https://example.com/async"])
    metadata_result = await client.get_metadata("https://example.com/async")

    assert submit_result.success is True
    assert batch_result.success_count == 1
    assert metadata_result.success is True
