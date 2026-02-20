"""Tests for Google API error parsing and retry helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

from seo_indexing_tracker.services.google_errors import (
    AuthenticationError,
    GoogleAPIError,
    InvalidURLError,
    QuotaExceededError,
    is_retryable_google_error,
    parse_google_http_error,
    retry_google_api_call,
)


def _http_error(status: int, reason: str, content: str) -> HttpError:
    response = SimpleNamespace(status=status, reason=reason)
    return HttpError(response, content.encode("utf-8"), uri=None)


def test_parse_google_http_error_extracts_quota_details() -> None:
    error = _http_error(
        429,
        "Too Many Requests",
        '{"error": {"code": 429, "message": "Quota exceeded", "errors": [{"reason": "quotaExceeded"}]}}',
    )

    parsed_error = parse_google_http_error(
        error,
        operation="urlNotifications.publish",
        service="indexing",
    )

    assert isinstance(parsed_error, QuotaExceededError)
    assert parsed_error.status_code == 429
    assert parsed_error.message == "Quota exceeded"
    assert parsed_error.details is not None
    assert parsed_error.details["code"] == 429


def test_parse_google_http_error_classifies_auth_and_invalid_url() -> None:
    auth_error = _http_error(
        403,
        "Forbidden",
        '{"error": {"message": "Insufficient authentication scopes", "errors": [{"reason": "insufficientPermissions"}]}}',
    )
    invalid_url_error = _http_error(
        400,
        "Bad Request",
        '{"error": {"message": "Invalid value for inspectionUrl", "errors": [{"reason": "invalidArgument"}]}}',
    )

    parsed_auth_error = parse_google_http_error(auth_error)
    parsed_invalid_url_error = parse_google_http_error(invalid_url_error)

    assert isinstance(parsed_auth_error, AuthenticationError)
    assert isinstance(parsed_invalid_url_error, InvalidURLError)


def test_retry_decorator_retries_transient_google_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = {"count": 0}
    monkeypatch.setattr(
        "seo_indexing_tracker.services.google_errors.sleep", lambda _: None
    )

    @retry_google_api_call(max_retries=2, base_delay_seconds=0.0)
    def flaky_request() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise _http_error(
                503,
                "Service Unavailable",
                '{"error": {"message": "Backend error", "errors": [{"reason": "backendError"}]}}',
            )
        return "ok"

    result = flaky_request()

    assert result == "ok"
    assert attempts["count"] == 3


def test_retry_decorator_fails_fast_for_non_retryable_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "seo_indexing_tracker.services.google_errors.sleep", lambda _: None
    )
    attempts = {"count": 0}

    @retry_google_api_call(max_retries=2, base_delay_seconds=0.0)
    def request() -> str:
        attempts["count"] += 1
        raise _http_error(
            400,
            "Bad Request",
            '{"error": {"message": "Invalid argument", "errors": [{"reason": "invalidArgument"}]}}',
        )

    with pytest.raises(GoogleAPIError):
        request()

    assert attempts["count"] == 1


def test_is_retryable_google_error_returns_expected_value() -> None:
    retryable_error = QuotaExceededError(
        "Quota exceeded",
        status_code=429,
        reason="Too Many Requests",
        details={"errors": [{"reason": "quotaExceeded"}]},
        operation="urlNotifications.publish",
        service="indexing",
    )
    non_retryable_error = InvalidURLError(
        "Invalid URL",
        status_code=400,
        reason="Bad Request",
        details={"errors": [{"reason": "invalidArgument"}]},
        operation="urlNotifications.publish",
        service="indexing",
    )

    assert is_retryable_google_error(retryable_error) is True
    assert is_retryable_google_error(non_retryable_error) is False
