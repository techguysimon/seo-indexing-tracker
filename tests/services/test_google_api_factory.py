"""Tests for website-scoped Google API client factory caching and isolation."""

from __future__ import annotations

import threading
import time
from uuid import UUID, uuid4

import pytest

from seo_indexing_tracker.services.google_api_factory import (
    GoogleAPIClientFactory,
    WebsiteServiceAccountConfig,
)


def test_get_client_caches_and_reuses_per_website() -> None:
    website_id = uuid4()
    loader_calls: list[UUID | str] = []

    def config_loader(website_key: UUID | str) -> WebsiteServiceAccountConfig:
        loader_calls.append(website_key)
        return WebsiteServiceAccountConfig(credentials_path="/tmp/site-a.json")

    factory = GoogleAPIClientFactory(config_loader=config_loader)

    first_client = factory.get_client(website_id)
    second_client = factory.get_client(website_id)

    assert first_client is second_client
    assert loader_calls == [website_id]


def test_get_client_uses_website_specific_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    indexing_calls: list[str] = []
    search_console_calls: list[str] = []

    class FakeIndexingClient:
        def __init__(self, *, credentials_path: str) -> None:
            indexing_calls.append(credentials_path)

    class FakeURLInspectionClient:
        def __init__(self, *, credentials_path: str) -> None:
            search_console_calls.append(credentials_path)

    monkeypatch.setattr(
        "seo_indexing_tracker.services.google_api_factory.GoogleIndexingClient",
        FakeIndexingClient,
    )
    monkeypatch.setattr(
        "seo_indexing_tracker.services.google_api_factory.GoogleURLInspectionClient",
        FakeURLInspectionClient,
    )

    path_by_website = {
        "site-a": "/tmp/site-a.json",
        "site-b": "/tmp/site-b.json",
    }

    def config_loader(website_key: UUID | str) -> WebsiteServiceAccountConfig:
        return WebsiteServiceAccountConfig(
            credentials_path=path_by_website[str(website_key)]
        )

    factory = GoogleAPIClientFactory(config_loader=config_loader)

    site_a_clients = factory.get_client("site-a")
    site_b_clients = factory.get_client("site-b")

    _ = site_a_clients.indexing
    _ = site_a_clients.search_console
    _ = site_b_clients.indexing
    _ = site_b_clients.search_console

    assert indexing_calls == ["/tmp/site-a.json", "/tmp/site-b.json"]
    assert search_console_calls == ["/tmp/site-a.json", "/tmp/site-b.json"]


def test_factory_and_bundle_lazily_initialize_google_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    indexing_init_count = 0
    search_console_init_count = 0

    class FakeIndexingClient:
        def __init__(self, *, credentials_path: str) -> None:
            del credentials_path
            nonlocal indexing_init_count
            indexing_init_count += 1

    class FakeURLInspectionClient:
        def __init__(self, *, credentials_path: str) -> None:
            del credentials_path
            nonlocal search_console_init_count
            search_console_init_count += 1

    monkeypatch.setattr(
        "seo_indexing_tracker.services.google_api_factory.GoogleIndexingClient",
        FakeIndexingClient,
    )
    monkeypatch.setattr(
        "seo_indexing_tracker.services.google_api_factory.GoogleURLInspectionClient",
        FakeURLInspectionClient,
    )

    factory = GoogleAPIClientFactory(
        config_loader=lambda _website_id: WebsiteServiceAccountConfig(
            credentials_path="/tmp/service-account.json"
        )
    )

    website_clients = factory.get_client("site-a")

    assert indexing_init_count == 0
    assert search_console_init_count == 0

    _ = website_clients.indexing
    _ = website_clients.indexing
    _ = website_clients.search_console
    _ = website_clients.search_console

    assert indexing_init_count == 1
    assert search_console_init_count == 1


def test_clear_cache_invalidates_cached_client_and_credentials_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_cache_clear_count = 0
    loader_call_count = 0

    def fake_clear_google_credentials_cache() -> None:
        nonlocal credential_cache_clear_count
        credential_cache_clear_count += 1

    def config_loader(_: UUID | str) -> WebsiteServiceAccountConfig:
        nonlocal loader_call_count
        loader_call_count += 1
        return WebsiteServiceAccountConfig(credentials_path="/tmp/site-a.json")

    monkeypatch.setattr(
        "seo_indexing_tracker.services.google_api_factory.clear_google_credentials_cache",
        fake_clear_google_credentials_cache,
    )

    factory = GoogleAPIClientFactory(config_loader=config_loader)

    first_client = factory.get_client("site-a")
    factory.clear_cache("site-a")
    second_client = factory.get_client("site-a")

    assert first_client is not second_client
    assert loader_call_count == 2
    assert credential_cache_clear_count == 1


def test_get_client_is_thread_safe_and_loads_configuration_once() -> None:
    website_id = uuid4()
    loader_call_count = 0
    loader_lock = threading.Lock()

    def config_loader(_: UUID | str) -> WebsiteServiceAccountConfig:
        nonlocal loader_call_count
        with loader_lock:
            loader_call_count += 1
        time.sleep(0.01)
        return WebsiteServiceAccountConfig(credentials_path="/tmp/site-a.json")

    factory = GoogleAPIClientFactory(config_loader=config_loader)

    thread_count = 12
    start_barrier = threading.Barrier(thread_count)
    results: list[object] = []
    results_lock = threading.Lock()

    def worker() -> None:
        start_barrier.wait()
        client = factory.get_client(website_id)
        with results_lock:
            results.append(client)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == thread_count
    assert loader_call_count == 1
    first_result = results[0]
    assert all(result is first_result for result in results)
