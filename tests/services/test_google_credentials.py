"""Tests for Google service account credential loading and caching."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from seo_indexing_tracker.services.google_credentials import (
    GoogleCredentialsError,
    clear_google_credentials_cache,
    load_service_account_credentials,
)


@pytest.fixture(autouse=True)
def clear_credentials_cache_between_tests() -> None:
    clear_google_credentials_cache()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _service_account_payload() -> dict[str, str]:
    return {
        "type": "service_account",
        "project_id": "project-id",
        "private_key_id": "private-key-id",
        "private_key": "-----BEGIN PRIVATE KEY-----\\nfake\\n-----END PRIVATE KEY-----\\n",
        "client_email": "service-account@example.com",
        "client_id": "1234567890",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


def test_load_service_account_credentials_caches_by_path_and_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials_path = tmp_path / "service-account.json"
    _write_json(credentials_path, _service_account_payload())

    calls: list[dict[str, Any]] = []

    def fake_from_service_account_info(
        info: dict[str, Any],
        scopes: list[str] | None = None,
    ) -> object:
        calls.append({"info": info, "scopes": scopes})
        return object()

    monkeypatch.setattr(
        "seo_indexing_tracker.services.google_credentials.service_account.Credentials.from_service_account_info",
        fake_from_service_account_info,
    )

    first_credentials = load_service_account_credentials(
        credentials_path,
        scopes=["scope-a", "scope-a", "scope-b"],
    )
    second_credentials = load_service_account_credentials(
        credentials_path,
        scopes=["scope-a", "scope-b"],
    )

    assert first_credentials is second_credentials
    assert len(calls) == 1
    assert calls[0]["scopes"] == ["scope-a", "scope-b"]


def test_load_service_account_credentials_supports_different_scope_sets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials_path = tmp_path / "service-account.json"
    _write_json(credentials_path, _service_account_payload())

    calls: list[list[str] | None] = []

    def fake_from_service_account_info(
        _: dict[str, Any],
        scopes: list[str] | None = None,
    ) -> object:
        calls.append(scopes)
        return object()

    monkeypatch.setattr(
        "seo_indexing_tracker.services.google_credentials.service_account.Credentials.from_service_account_info",
        fake_from_service_account_info,
    )

    indexing_credentials = load_service_account_credentials(
        credentials_path,
        scopes=["https://www.googleapis.com/auth/indexing"],
    )
    webmasters_credentials = load_service_account_credentials(
        credentials_path,
        scopes=["https://www.googleapis.com/auth/webmasters"],
    )

    assert indexing_credentials is not webmasters_credentials
    assert calls == [
        ["https://www.googleapis.com/auth/indexing"],
        ["https://www.googleapis.com/auth/webmasters"],
    ]


def test_load_service_account_credentials_raises_for_missing_file(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing.json"

    with pytest.raises(GoogleCredentialsError, match="does not exist"):
        load_service_account_credentials(missing_path)


def test_load_service_account_credentials_raises_for_invalid_json(
    tmp_path: Path,
) -> None:
    credentials_path = tmp_path / "service-account.json"
    credentials_path.write_text("{not-json}", encoding="utf-8")

    with pytest.raises(GoogleCredentialsError, match="invalid JSON"):
        load_service_account_credentials(credentials_path)


def test_load_service_account_credentials_raises_for_missing_required_fields(
    tmp_path: Path,
) -> None:
    credentials_path = tmp_path / "service-account.json"
    _write_json(
        credentials_path,
        {
            "type": "service_account",
            "project_id": "project-id",
        },
    )

    with pytest.raises(GoogleCredentialsError, match="missing required fields"):
        load_service_account_credentials(credentials_path)


def test_load_service_account_credentials_raises_for_empty_scope_value(
    tmp_path: Path,
) -> None:
    credentials_path = tmp_path / "service-account.json"
    _write_json(credentials_path, _service_account_payload())

    with pytest.raises(GoogleCredentialsError, match="empty values"):
        load_service_account_credentials(credentials_path, scopes=[" "])
