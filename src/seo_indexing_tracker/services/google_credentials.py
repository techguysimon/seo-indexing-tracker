"""Google service account credential loading with validation and caching."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

from google.oauth2 import service_account

REQUIRED_SERVICE_ACCOUNT_FIELDS = frozenset(
    {
        "type",
        "project_id",
        "private_key_id",
        "private_key",
        "client_email",
        "client_id",
        "token_uri",
    }
)


class GoogleCredentialsError(Exception):
    """Base exception for service account credential loading failures."""


def _normalize_scopes(scopes: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if scopes is None:
        return ()

    normalized_scopes: list[str] = []
    seen_scopes: set[str] = set()
    for scope in scopes:
        stripped_scope = scope.strip()
        if stripped_scope == "":
            raise GoogleCredentialsError(
                "Service account scopes cannot contain empty values"
            )

        if stripped_scope in seen_scopes:
            continue

        normalized_scopes.append(stripped_scope)
        seen_scopes.add(stripped_scope)

    return tuple(normalized_scopes)


def _resolve_credentials_path(credentials_path: str | Path) -> Path:
    resolved_path = Path(credentials_path).expanduser().resolve()
    if resolved_path.is_file():
        return resolved_path

    raise GoogleCredentialsError(
        f"Service account credential file does not exist: {resolved_path}"
    )


def _validate_credentials_payload(
    *, payload: Any, credentials_path: Path
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise GoogleCredentialsError(
            f"Service account credential file must contain a JSON object: {credentials_path}"
        )

    missing_fields = sorted(REQUIRED_SERVICE_ACCOUNT_FIELDS.difference(payload.keys()))
    if missing_fields:
        missing_fields_text = ", ".join(missing_fields)
        raise GoogleCredentialsError(
            "Service account credential file is missing required fields "
            f"({missing_fields_text}): {credentials_path}"
        )

    if payload.get("type") != "service_account":
        raise GoogleCredentialsError(
            "Service account credential file must have type='service_account': "
            f"{credentials_path}"
        )

    return payload


@lru_cache(maxsize=64)
def _read_and_validate_credentials_payload(credentials_path: str) -> dict[str, Any]:
    resolved_path = _resolve_credentials_path(credentials_path)

    try:
        with resolved_path.open("r", encoding="utf-8") as credentials_file:
            parsed_payload: Any = json.load(credentials_file)
    except OSError as error:
        raise GoogleCredentialsError(
            f"Unable to read service account credential file {resolved_path}: {error}"
        ) from error
    except json.JSONDecodeError as error:
        raise GoogleCredentialsError(
            "Service account credential file contains invalid JSON "
            f"at line {error.lineno}, column {error.colno}: {resolved_path}"
        ) from error

    return _validate_credentials_payload(
        payload=parsed_payload,
        credentials_path=resolved_path,
    )


@lru_cache(maxsize=128)
def _build_cached_credentials(
    credentials_path: str,
    scopes: tuple[str, ...],
) -> service_account.Credentials:
    payload = _read_and_validate_credentials_payload(credentials_path)
    requested_scopes = list(scopes) if scopes else None

    try:
        credentials = service_account.Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
            payload,
            scopes=requested_scopes,
        )
        return cast(service_account.Credentials, credentials)
    except Exception as error:
        raise GoogleCredentialsError(
            "Unable to construct Google service account credentials from "
            f"{credentials_path}: {error}"
        ) from error


def load_service_account_credentials(
    credentials_path: str | Path,
    *,
    scopes: list[str] | tuple[str, ...] | None = None,
) -> service_account.Credentials:
    """Load and cache Google service account credentials for the given scopes."""

    resolved_path = _resolve_credentials_path(credentials_path)
    normalized_scopes = _normalize_scopes(scopes)
    return _build_cached_credentials(str(resolved_path), normalized_scopes)


def clear_google_credentials_cache() -> None:
    """Clear in-memory cache for loaded credential files and credential objects."""

    _read_and_validate_credentials_payload.cache_clear()
    _build_cached_credentials.cache_clear()


__all__ = [
    "GoogleCredentialsError",
    "clear_google_credentials_cache",
    "load_service_account_credentials",
]
