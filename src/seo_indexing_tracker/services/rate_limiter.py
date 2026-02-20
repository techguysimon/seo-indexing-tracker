"""Per-website token bucket and concurrency rate limiting service."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.models import RateLimitState, Website
from seo_indexing_tracker.services.quota_service import (
    DailyQuotaExceededError,
    QuotaService,
)

SessionScopeFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True)
class WebsiteRateLimitConfig:
    """Parsed rate limit configuration for a website."""

    bucket_size: int
    refill_rate: float
    max_concurrent_requests: int
    queue_excess_requests: bool


class RateLimitTokenUnavailableError(RuntimeError):
    """Raised when no token is currently available and blocking is disabled."""


class RateLimitTimeoutError(RuntimeError):
    """Raised when waiting for concurrency or token availability timed out."""


class ConcurrentRequestLimitExceededError(RuntimeError):
    """Raised when concurrency is exhausted and queueing is disabled."""


@dataclass
class RateLimitPermit:
    """Represents a held concurrency slot that must be released."""

    _semaphore: asyncio.Semaphore
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return

        self._semaphore.release()
        self._released = True

    async def __aenter__(self) -> RateLimitPermit:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.release()


class RateLimiterService:
    """Coordinates token bucket, concurrency, and quota limits per website."""

    def __init__(
        self,
        *,
        quota_service: QuotaService,
        session_factory: SessionScopeFactory | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        if session_factory is None:
            from seo_indexing_tracker.database import session_scope

            session_factory = session_scope

        self._session_factory = session_factory
        self._quota_service = quota_service
        self._now_factory = now_factory or self._default_now
        self._token_locks: dict[UUID, asyncio.Lock] = {}
        self._semaphore_lock = asyncio.Lock()
        self._semaphores: dict[UUID, tuple[int, asyncio.Semaphore]] = {}

    async def acquire(
        self,
        website_id: UUID,
        *,
        api_type: str,
        block_until_token_available: bool = True,
        queue_excess_requests: bool | None = None,
        timeout_seconds: float | None = None,
    ) -> RateLimitPermit:
        """Acquire concurrency, token, and quota capacity for one request."""

        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

        config = await self._get_website_config(website_id)
        should_queue_requests = (
            config.queue_excess_requests
            if queue_excess_requests is None
            else queue_excess_requests
        )
        semaphore = await self._get_or_create_semaphore(website_id, config)

        await self._acquire_concurrency_slot(
            website_id=website_id,
            semaphore=semaphore,
            should_queue_requests=should_queue_requests,
            timeout_seconds=timeout_seconds,
        )
        permit = RateLimitPermit(_semaphore=semaphore)

        try:
            has_quota = await self._quota_service.check_quota_available(
                website_id, api_type, 1
            )
            if not has_quota:
                raise DailyQuotaExceededError(
                    f"Daily {api_type} quota exhausted for website {website_id}"
                )

            await self._consume_token(
                website_id=website_id,
                config=config,
                block_until_token_available=block_until_token_available,
                timeout_seconds=timeout_seconds,
            )

            try:
                await self._quota_service.increment_usage(website_id, api_type)
            except DailyQuotaExceededError:
                await self._refund_token(website_id=website_id, config=config)
                raise
        except Exception:
            permit.release()
            raise

        return permit

    @asynccontextmanager
    async def limit(
        self,
        website_id: UUID,
        *,
        api_type: str,
        block_until_token_available: bool = True,
        queue_excess_requests: bool | None = None,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[RateLimitPermit]:
        """Context manager wrapper around ``acquire`` for safe release."""

        permit = await self.acquire(
            website_id,
            api_type=api_type,
            block_until_token_available=block_until_token_available,
            queue_excess_requests=queue_excess_requests,
            timeout_seconds=timeout_seconds,
        )
        try:
            yield permit
        finally:
            permit.release()

    async def _get_website_config(self, website_id: UUID) -> WebsiteRateLimitConfig:
        async with self._session_factory() as session:
            website = await session.get(Website, website_id)
            if website is None:
                raise ValueError(f"Website {website_id} does not exist")

            return self._parse_website_config(website)

    @staticmethod
    def _parse_website_config(website: Website) -> WebsiteRateLimitConfig:
        if website.rate_limit_bucket_size <= 0:
            raise RuntimeError(
                f"Website {website.id} has invalid bucket size: "
                f"{website.rate_limit_bucket_size}"
            )

        if website.rate_limit_refill_rate <= 0:
            raise RuntimeError(
                f"Website {website.id} has invalid refill rate: "
                f"{website.rate_limit_refill_rate}"
            )

        if website.rate_limit_max_concurrent_requests <= 0:
            raise RuntimeError(
                f"Website {website.id} has invalid max concurrency: "
                f"{website.rate_limit_max_concurrent_requests}"
            )

        return WebsiteRateLimitConfig(
            bucket_size=website.rate_limit_bucket_size,
            refill_rate=website.rate_limit_refill_rate,
            max_concurrent_requests=website.rate_limit_max_concurrent_requests,
            queue_excess_requests=website.rate_limit_queue_excess_requests,
        )

    async def _get_or_create_semaphore(
        self,
        website_id: UUID,
        config: WebsiteRateLimitConfig,
    ) -> asyncio.Semaphore:
        async with self._semaphore_lock:
            known = self._semaphores.get(website_id)
            if known is None:
                semaphore = asyncio.Semaphore(config.max_concurrent_requests)
                self._semaphores[website_id] = (
                    config.max_concurrent_requests,
                    semaphore,
                )
                return semaphore

            known_limit, semaphore = known
            if known_limit == config.max_concurrent_requests:
                return semaphore

            replacement = asyncio.Semaphore(config.max_concurrent_requests)
            self._semaphores[website_id] = (config.max_concurrent_requests, replacement)
            return replacement

    async def _acquire_concurrency_slot(
        self,
        *,
        website_id: UUID,
        semaphore: asyncio.Semaphore,
        should_queue_requests: bool,
        timeout_seconds: float | None,
    ) -> None:
        if should_queue_requests:
            await self._acquire_with_timeout(semaphore, timeout_seconds)
            return

        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=0.0)
        except TimeoutError as error:
            raise ConcurrentRequestLimitExceededError(
                f"Concurrent request limit reached for website {website_id}"
            ) from error

    @staticmethod
    async def _acquire_with_timeout(
        semaphore: asyncio.Semaphore,
        timeout_seconds: float | None,
    ) -> None:
        if timeout_seconds is None:
            await semaphore.acquire()
            return

        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=timeout_seconds)
        except TimeoutError as error:
            raise RateLimitTimeoutError(
                "Timed out waiting for concurrency slot"
            ) from error

    async def _consume_token(
        self,
        *,
        website_id: UUID,
        config: WebsiteRateLimitConfig,
        block_until_token_available: bool,
        timeout_seconds: float | None,
    ) -> None:
        deadline = (
            None
            if timeout_seconds is None
            else asyncio.get_running_loop().time() + timeout_seconds
        )

        while True:
            wait_seconds = await self._try_consume_token_once(
                website_id=website_id,
                config=config,
            )
            if wait_seconds == 0:
                return

            if not block_until_token_available:
                raise RateLimitTokenUnavailableError(
                    f"No token currently available for website {website_id}"
                )

            if deadline is not None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise RateLimitTimeoutError("Timed out waiting for token refill")
                wait_seconds = min(wait_seconds, remaining)

            await asyncio.sleep(wait_seconds)

    async def _try_consume_token_once(
        self,
        *,
        website_id: UUID,
        config: WebsiteRateLimitConfig,
    ) -> float:
        token_lock = self._token_locks.setdefault(website_id, asyncio.Lock())
        async with token_lock:
            now = self._now_factory()
            async with self._session_factory() as session:
                state = await self._get_or_create_state_for_update(
                    session=session,
                    website_id=website_id,
                    now=now,
                    config=config,
                )
                available_tokens = self._refill_tokens(
                    state=state,
                    bucket_size=config.bucket_size,
                    refill_rate=config.refill_rate,
                    now=now,
                )
                state.last_refill_at = now
                if available_tokens >= 1:
                    state.token_count = available_tokens - 1
                    await session.flush()
                    return 0.0

                state.token_count = available_tokens
                await session.flush()
                return (1 - available_tokens) / config.refill_rate

    async def _refund_token(
        self,
        *,
        website_id: UUID,
        config: WebsiteRateLimitConfig,
    ) -> None:
        token_lock = self._token_locks.setdefault(website_id, asyncio.Lock())
        async with token_lock:
            now = self._now_factory()
            async with self._session_factory() as session:
                state = await self._get_or_create_state_for_update(
                    session=session,
                    website_id=website_id,
                    now=now,
                    config=config,
                )
                available_tokens = self._refill_tokens(
                    state=state,
                    bucket_size=config.bucket_size,
                    refill_rate=config.refill_rate,
                    now=now,
                )
                state.token_count = min(available_tokens + 1, float(config.bucket_size))
                state.last_refill_at = now
                await session.flush()

    async def _get_or_create_state_for_update(
        self,
        *,
        session: AsyncSession,
        website_id: UUID,
        now: datetime,
        config: WebsiteRateLimitConfig,
    ) -> RateLimitState:
        state = await session.scalar(
            select(RateLimitState)
            .where(RateLimitState.website_id == website_id)
            .with_for_update()
        )
        if state is not None:
            return state

        state = RateLimitState(
            website_id=website_id,
            token_count=float(config.bucket_size),
            last_refill_at=now,
        )
        session.add(state)
        await session.flush()
        return state

    @staticmethod
    def _refill_tokens(
        *,
        state: RateLimitState,
        bucket_size: int,
        refill_rate: float,
        now: datetime,
    ) -> float:
        normalized_now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        last_refill_at = (
            state.last_refill_at
            if state.last_refill_at.tzinfo is not None
            else state.last_refill_at.replace(tzinfo=UTC)
        )
        elapsed_seconds = max((normalized_now - last_refill_at).total_seconds(), 0.0)
        refilled = state.token_count + (elapsed_seconds * refill_rate)
        return min(refilled, float(bucket_size))

    @staticmethod
    def _default_now() -> datetime:
        return datetime.now(UTC)


__all__ = [
    "ConcurrentRequestLimitExceededError",
    "RateLimiterService",
    "RateLimitPermit",
    "RateLimitTimeoutError",
    "RateLimitTokenUnavailableError",
    "WebsiteRateLimitConfig",
]
