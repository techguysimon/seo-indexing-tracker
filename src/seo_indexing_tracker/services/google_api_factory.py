"""Factory for website-scoped Google API client bundles."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from uuid import UUID

from seo_indexing_tracker.services.google_credentials import (
    clear_google_credentials_cache,
)
from seo_indexing_tracker.services.google_indexing_client import GoogleIndexingClient
from seo_indexing_tracker.services.google_url_inspection_client import (
    GoogleURLInspectionClient,
)


@dataclass(slots=True, frozen=True)
class WebsiteServiceAccountConfig:
    """Service account configuration used to build website-scoped clients."""

    credentials_path: str


ServiceAccountConfigLoader = Callable[[UUID | str], WebsiteServiceAccountConfig]


class WebsiteGoogleAPIClients:
    """Lazy Google API clients for a single website/service account."""

    def __init__(self, *, config: WebsiteServiceAccountConfig) -> None:
        self._credentials_path = str(
            Path(config.credentials_path).expanduser().resolve()
        )
        self._indexing_client: GoogleIndexingClient | None = None
        self._url_inspection_client: GoogleURLInspectionClient | None = None
        self._lock = Lock()

    @property
    def indexing(self) -> GoogleIndexingClient:
        """Get (or lazily initialize) website-scoped Indexing API client."""

        if self._indexing_client is not None:
            return self._indexing_client

        with self._lock:
            if self._indexing_client is None:
                self._indexing_client = GoogleIndexingClient(
                    credentials_path=self._credentials_path
                )
        return self._indexing_client

    @property
    def search_console(self) -> GoogleURLInspectionClient:
        """Get (or lazily initialize) website-scoped Search Console client."""

        if self._url_inspection_client is not None:
            return self._url_inspection_client

        with self._lock:
            if self._url_inspection_client is None:
                self._url_inspection_client = GoogleURLInspectionClient(
                    credentials_path=self._credentials_path
                )
        return self._url_inspection_client


class GoogleAPIClientFactory:
    """Thread-safe cache for website-scoped Google API client bundles."""

    def __init__(self, *, config_loader: ServiceAccountConfigLoader) -> None:
        self._config_loader = config_loader
        self._clients: dict[str, WebsiteGoogleAPIClients] = {}
        self._lock = Lock()

    def get_client(self, website_id: UUID | str) -> WebsiteGoogleAPIClients:
        """Return a cached client bundle for the requested website."""

        cache_key = str(website_id)
        if cache_key in self._clients:
            return self._clients[cache_key]

        with self._lock:
            cached_client = self._clients.get(cache_key)
            if cached_client is not None:
                return cached_client

            config = self._config_loader(website_id)
            client_bundle = WebsiteGoogleAPIClients(config=config)
            self._clients[cache_key] = client_bundle
            return client_bundle

    def clear_cache(self, website_id: UUID | str | None = None) -> None:
        """Clear cached clients for one website or all websites."""

        with self._lock:
            if website_id is None:
                self._clients.clear()
            else:
                self._clients.pop(str(website_id), None)

        clear_google_credentials_cache()


__all__ = [
    "GoogleAPIClientFactory",
    "ServiceAccountConfigLoader",
    "WebsiteGoogleAPIClients",
    "WebsiteServiceAccountConfig",
]
