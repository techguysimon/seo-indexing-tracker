"""Validation helpers for configuration resources before persistence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
from pathlib import Path

import httpx

from seo_indexing_tracker.config import get_settings
from seo_indexing_tracker.services.google_credentials import (
    GoogleCredentialsError,
    load_service_account_credentials,
)

DEFAULT_CONFIG_VALIDATION_TIMEOUT_SECONDS = 10.0
DEFAULT_CONFIG_VALIDATION_MAX_REDIRECTS = 5

logger = logging.getLogger(__name__)


class ConfigurationValidationError(Exception):
    """Raised when configuration data fails validation checks."""


class ConfigurationValidationService:
    """Validate service account files and website-related URLs."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_CONFIG_VALIDATION_TIMEOUT_SECONDS,
        max_redirects: int = DEFAULT_CONFIG_VALIDATION_MAX_REDIRECTS,
        user_agent: str | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

        if max_redirects < 0:
            raise ValueError("max_redirects must be zero or greater")

        self._http_client = http_client
        self._timeout_seconds = timeout_seconds
        self._max_redirects = max_redirects
        self._user_agent = user_agent or get_settings().OUTBOUND_HTTP_USER_AGENT

    async def validate_service_account(
        self,
        *,
        credentials_path: str,
        scopes: list[str] | tuple[str, ...] | None = None,
    ) -> str:
        """Validate credential JSON by loading Google service account credentials."""

        resolved_path = Path(credentials_path).expanduser().resolve()
        try:
            load_service_account_credentials(resolved_path, scopes=scopes)
        except GoogleCredentialsError as error:
            raise ConfigurationValidationError(str(error)) from error

        return str(resolved_path)

    async def validate_sitemap_url(self, *, sitemap_url: str) -> str:
        """Validate sitemap URL accessibility by issuing an HTTP HEAD request."""

        await self._validate_url_reachable(
            url=sitemap_url,
            field_name="sitemap_url",
            resource_name="Sitemap URL",
        )
        return sitemap_url

    async def validate_website_url(self, *, site_url: str) -> str:
        """Validate website URL accessibility by issuing an HTTP HEAD request.

        Accepts two formats:
        - Normal URLs: https://domain.com (validates via HTTP request)
        - Domain properties: sc-domain:domain.com (validates format only)
        """

        # Domain properties (sc-domain:example.com) don't need HTTP reachability check
        if site_url.lower().startswith("sc-domain:"):
            domain_part = site_url[10:].strip()  # Extract domain after "sc-domain:"
            if not domain_part:
                raise ConfigurationValidationError(
                    "sc-domain: format requires a domain name (e.g., sc-domain:example.com)"
                )
            # Basic domain validation - should have at least one dot
            if "." not in domain_part:
                raise ConfigurationValidationError(
                    f"Invalid domain in sc-domain: format: {domain_part}"
                )
            return site_url

        # Normal URLs require HTTP reachability validation
        await self._validate_url_reachable(
            url=site_url,
            field_name="site_url",
            resource_name="Website URL",
        )
        return site_url

    @asynccontextmanager
    async def _http_client_context(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._http_client is not None:
            yield self._http_client
            return

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
            follow_redirects=True,
            max_redirects=self._max_redirects,
        ) as http_client:
            yield http_client

    async def _validate_url_reachable(
        self,
        *,
        url: str,
        field_name: str,
        resource_name: str,
    ) -> None:
        async with self._http_client_context() as http_client:
            response = await self._request(
                http_client=http_client,
                method="HEAD",
                url=url,
                field_name=field_name,
                resource_name=resource_name,
            )

            if response.status_code in {
                httpx.codes.FORBIDDEN,
                httpx.codes.METHOD_NOT_ALLOWED,
                httpx.codes.NOT_IMPLEMENTED,
            }:
                response = await self._request(
                    http_client=http_client,
                    method="GET",
                    url=url,
                    field_name=field_name,
                    resource_name=resource_name,
                )

            if response.status_code < 400:
                return

            if response.status_code == httpx.codes.FORBIDDEN:
                logger.warning(
                    "%s returned HTTP 403 during reachability validation; allowing configuration: %s",
                    resource_name,
                    url,
                )
                return

            raise ConfigurationValidationError(
                f"{resource_name} is not reachable (HTTP {response.status_code}): {url}"
            )

    async def _request(
        self,
        *,
        http_client: httpx.AsyncClient,
        method: str,
        url: str,
        field_name: str,
        resource_name: str,
    ) -> httpx.Response:
        try:
            return await http_client.request(
                method,
                url,
                headers={"User-Agent": self._user_agent},
            )
        except httpx.InvalidURL as error:
            raise ConfigurationValidationError(
                f"{field_name} must be a valid HTTP URL: {error}"
            ) from error
        except httpx.TimeoutException as error:
            raise ConfigurationValidationError(
                f"Timed out validating {resource_name.lower()}: {url}"
            ) from error
        except httpx.RequestError as error:
            raise ConfigurationValidationError(
                f"Unable to reach {resource_name.lower()} {url}: {error}"
            ) from error


__all__ = [
    "ConfigurationValidationError",
    "ConfigurationValidationService",
    "DEFAULT_CONFIG_VALIDATION_MAX_REDIRECTS",
    "DEFAULT_CONFIG_VALIDATION_TIMEOUT_SECONDS",
]
