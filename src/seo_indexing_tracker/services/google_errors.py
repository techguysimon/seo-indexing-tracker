"""Google API error parsing, classification, and retry utilities."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from functools import wraps
from time import sleep
from typing import Any, ParamSpec, TypeVar, cast

from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

TRANSIENT_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
TRANSIENT_ERROR_REASONS = frozenset(
    {
        "backenderror",
        "internalerror",
        "ratelimitexceeded",
        "userratelimitexceeded",
        "quotaexceeded",
    }
)

_LOGGER = logging.getLogger("seo_indexing_tracker.google_api")

P = ParamSpec("P")
R = TypeVar("R")


class GoogleAPIError(Exception):
    """Base Google API exception with parsed response context."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None,
        reason: str | None,
        details: dict[str, Any] | None,
        operation: str | None,
        service: str | None,
        retry_after_seconds: int | None,
    ) -> None:
        self.message = message
        self.status_code = status_code
        self.reason = reason
        self.details = details
        self.operation = operation
        self.service = service
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class QuotaExceededError(GoogleAPIError):
    """Raised when a Google API quota or rate limit is exceeded."""


class AuthenticationError(GoogleAPIError):
    """Raised when Google API authentication/authorization fails."""


class InvalidURLError(GoogleAPIError):
    """Raised when a URL argument is invalid for a Google API call."""


def _extract_status_code(error: HttpError) -> int | None:
    if hasattr(error, "status_code"):
        return cast(int | None, getattr(error, "status_code"))

    response = getattr(error, "resp", None)
    if response is None:
        return None

    return cast(int | None, getattr(response, "status", None))


def _extract_payload_text(error: HttpError) -> str:
    content = getattr(error, "content", b"")
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    if isinstance(content, str):
        return content
    return ""


def _parse_payload_details(payload_text: str) -> dict[str, Any] | None:
    if payload_text.strip() == "":
        return None

    try:
        parsed_payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed_payload, dict):
        return None

    error_payload = parsed_payload.get("error")
    if isinstance(error_payload, dict):
        return cast(dict[str, Any], error_payload)

    return cast(dict[str, Any], parsed_payload)


def _extract_retry_after_seconds(error: HttpError) -> int | None:
    response = getattr(error, "resp", None)
    if response is None:
        return None

    retry_after: Any | None = None
    if hasattr(response, "get"):
        try:
            retry_after = response.get("retry-after")
            if retry_after is None:
                retry_after = response.get("Retry-After")
        except Exception:
            retry_after = None

    if retry_after is None and hasattr(response, "headers"):
        headers = getattr(response, "headers")
        if hasattr(headers, "get"):
            retry_after = headers.get("retry-after") or headers.get("Retry-After")

    if retry_after is None:
        return None

    retry_after_text = str(retry_after).strip()
    if not retry_after_text:
        return None
    if retry_after_text.isdigit():
        return int(retry_after_text)
    return None


def _error_reasons(details: dict[str, Any] | None) -> set[str]:
    if details is None:
        return set()

    reasons: set[str] = set()
    reason_value = details.get("reason")
    if isinstance(reason_value, str):
        reasons.add(reason_value.strip().lower())

    errors = details.get("errors")
    if not isinstance(errors, list):
        return reasons

    for item in errors:
        if not isinstance(item, dict):
            continue
        item_reason = item.get("reason")
        if isinstance(item_reason, str):
            reasons.add(item_reason.strip().lower())

    return reasons


def _is_quota_error(
    *,
    status_code: int | None,
    reasons: set[str],
    message: str,
    reason_text: str,
) -> bool:
    diagnostic_text = f"{reason_text} {message}".lower()
    return (
        status_code == 429
        or any(reason in TRANSIENT_ERROR_REASONS for reason in reasons)
        or "quota" in diagnostic_text
        or "rate limit" in diagnostic_text
    )


def _is_authentication_error(status_code: int | None, reasons: set[str]) -> bool:
    auth_reasons = {
        "autherror",
        "forbidden",
        "insufficientpermissions",
        "insufficientauthenticationscopes",
        "unauthorized",
    }
    return status_code in {401, 403} and (
        len(reasons) == 0 or any(reason in auth_reasons for reason in reasons)
    )


def _is_invalid_url_error(
    *,
    status_code: int | None,
    reasons: set[str],
    message: str,
    reason_text: str,
) -> bool:
    if status_code not in {400, 422}:
        return False

    diagnostic_text = f"{reason_text} {message}".lower()
    if any(
        reason in {"invalid", "invalidargument", "parseerror"} for reason in reasons
    ):
        return "url" in diagnostic_text

    return any(
        marker in diagnostic_text for marker in ("url", "inspectionurl", "siteurl")
    )


def parse_google_http_error(
    error: HttpError,
    *,
    operation: str | None = None,
    service: str | None = None,
) -> GoogleAPIError:
    """Parse a googleapiclient HttpError into a typed GoogleAPIError."""

    status_code = _extract_status_code(error)
    reason_text = str(getattr(error, "reason", "") or "")
    payload_text = _extract_payload_text(error)
    payload_details = _parse_payload_details(payload_text)
    retry_after_seconds = _extract_retry_after_seconds(error)
    message = str(payload_details.get("message")) if payload_details else str(error)
    reasons = _error_reasons(payload_details)

    exception_type: type[GoogleAPIError] = GoogleAPIError
    if _is_quota_error(
        status_code=status_code,
        reasons=reasons,
        message=message,
        reason_text=reason_text,
    ):
        exception_type = QuotaExceededError
    elif _is_authentication_error(status_code, reasons):
        exception_type = AuthenticationError
    elif _is_invalid_url_error(
        status_code=status_code,
        reasons=reasons,
        message=message,
        reason_text=reason_text,
    ):
        exception_type = InvalidURLError

    parsed_error = exception_type(
        message,
        status_code=status_code,
        reason=reason_text or None,
        details=payload_details,
        operation=operation,
        service=service,
        retry_after_seconds=retry_after_seconds,
    )
    _LOGGER.error(
        "google_api_http_error",
        extra={
            "service": service,
            "operation": operation,
            "status_code": status_code,
            "error_type": parsed_error.__class__.__name__,
            "error_message": parsed_error.message,
        },
    )
    return parsed_error


def is_retryable_google_error(error: GoogleAPIError) -> bool:
    """Return whether a parsed GoogleAPIError should be retried."""

    if isinstance(error, QuotaExceededError):
        return True

    status_code = error.status_code
    if status_code is not None and status_code in TRANSIENT_HTTP_STATUS_CODES:
        return True

    reasons = _error_reasons(error.details)
    return any(reason in TRANSIENT_ERROR_REASONS for reason in reasons)


def retry_google_api_call(
    *,
    max_retries: int = 3,
    base_delay_seconds: float = 0.1,
    operation: str | None = None,
    service: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Retry decorator for transient Google API HttpError failures."""

    if max_retries < 0:
        raise ValueError("max_retries must be zero or greater")
    if base_delay_seconds < 0:
        raise ValueError("base_delay_seconds must be zero or greater")

    def decorator(function: Callable[P, R]) -> Callable[P, R]:
        @wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            for attempt in range(max_retries + 1):
                try:
                    return function(*args, **kwargs)
                except HttpError as error:
                    parsed_error = parse_google_http_error(
                        error,
                        operation=operation,
                        service=service,
                    )
                    should_retry = attempt < max_retries and is_retryable_google_error(
                        parsed_error
                    )
                    if not should_retry:
                        raise parsed_error from error
                    _LOGGER.warning(
                        "google_api_retrying",
                        extra={
                            "service": service,
                            "operation": operation,
                            "attempt": attempt + 1,
                            "max_retries": max_retries,
                            "status_code": parsed_error.status_code,
                        },
                    )
                except GoogleAPIError as error:
                    should_retry = attempt < max_retries and is_retryable_google_error(
                        error
                    )
                    if not should_retry:
                        raise
                    _LOGGER.warning(
                        "google_api_retrying",
                        extra={
                            "service": service,
                            "operation": operation,
                            "attempt": attempt + 1,
                            "max_retries": max_retries,
                            "status_code": error.status_code,
                        },
                    )

                sleep(base_delay_seconds * (2**attempt))

            raise RuntimeError("retry wrapper exited unexpectedly")

        return wrapper

    return decorator


def execute_with_google_retry(
    request: Callable[[], R],
    *,
    operation: str,
    service: str,
    max_retries: int = 3,
    base_delay_seconds: float = 0.1,
) -> R:
    """Execute a Google API request callable with transient retry semantics."""

    @retry_google_api_call(
        max_retries=max_retries,
        base_delay_seconds=base_delay_seconds,
        operation=operation,
        service=service,
    )
    def _execute() -> R:
        return request()

    return _execute()


__all__ = [
    "AuthenticationError",
    "GoogleAPIError",
    "InvalidURLError",
    "QuotaExceededError",
    "execute_with_google_retry",
    "is_retryable_google_error",
    "parse_google_http_error",
    "retry_google_api_call",
]
