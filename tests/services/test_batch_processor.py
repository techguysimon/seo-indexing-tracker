"""Tests for batched queue dequeue, submit, inspection, and progress tracking."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from seo_indexing_tracker.models import (
    Base,
    IndexStatus,
    IndexVerdict,
    SubmissionAction,
    SubmissionLog,
    SubmissionStatus,
    URL,
    Website,
)
from seo_indexing_tracker.services.batch_processor import (
    BatchProcessorService,
    BatchProcessorStatus,
    BatchProgressUpdate,
)
from seo_indexing_tracker.services.google_indexing_client import (
    BatchSubmitResult,
    IndexingURLResult,
)
from seo_indexing_tracker.services.google_url_inspection_client import (
    IndexStatusResult,
    InspectionSystemStatus,
)
from seo_indexing_tracker.services.priority_queue import PriorityQueueService


class _FakeRateLimitPermit:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class _FakeRateLimiter:
    def __init__(self) -> None:
        self.acquired_api_types: list[str] = []

    async def acquire(
        self,
        website_id: UUID,
        *,
        api_type: str,
    ) -> _FakeRateLimitPermit:
        del website_id
        self.acquired_api_types.append(api_type)
        return _FakeRateLimitPermit()


class _FakeIndexingClient:
    async def batch_submit(
        self,
        urls: list[str] | tuple[str, ...],
        action: str = "URL_UPDATED",
    ) -> BatchSubmitResult:
        results: list[IndexingURLResult] = []
        for url in urls:
            if url.endswith("/submit-fail"):
                results.append(
                    IndexingURLResult(
                        url=url,
                        action=action,
                        success=False,
                        http_status=429,
                        metadata=None,
                        error_code="QUOTA_EXCEEDED",
                        error_message="quota exhausted",
                        retry_after_seconds=None,
                    )
                )
                continue

            results.append(
                IndexingURLResult(
                    url=url,
                    action=action,
                    success=True,
                    http_status=200,
                    metadata={"url": url},
                    error_code=None,
                    error_message=None,
                    retry_after_seconds=None,
                )
            )

        success_count = sum(1 for item in results if item.success)
        return BatchSubmitResult(
            action=action,
            total_urls=len(results),
            success_count=success_count,
            failure_count=len(results) - success_count,
            results=results,
        )


class _FakeInspectionClient:
    async def inspect_url(self, url: str, site_url: str) -> IndexStatusResult:
        if url.endswith("/inspect-fail"):
            return IndexStatusResult(
                inspection_url=url,
                site_url=site_url,
                success=False,
                http_status=503,
                system_status=InspectionSystemStatus.ERROR,
                verdict="FAIL",
                coverage_state="Server error (5xx)",
                last_crawl_time=None,
                indexing_state=None,
                robots_txt_state=None,
                raw_response=None,
                error_code="API_ERROR",
                error_message="inspection failed",
                retry_after_seconds=None,
            )

        return IndexStatusResult(
            inspection_url=url,
            site_url=site_url,
            success=True,
            http_status=200,
            system_status=InspectionSystemStatus.INDEXED,
            verdict="PASS",
            coverage_state="Submitted and indexed",
            last_crawl_time=datetime(2026, 2, 20, 9, 0, tzinfo=UTC),
            indexing_state="INDEXING_ALLOWED",
            robots_txt_state="ALLOWED",
            raw_response={
                "inspectionResult": {
                    "indexStatusResult": {
                        "googleCanonical": url,
                        "userCanonical": url,
                    }
                }
            },
            error_code=None,
            error_message=None,
            retry_after_seconds=None,
        )


class _FakeClientBundle:
    def __init__(self) -> None:
        self.indexing = _FakeIndexingClient()
        self.search_console = _FakeInspectionClient()


class _FakeClientFactory:
    def get_client(self, website_id: UUID | str) -> _FakeClientBundle:
        del website_id
        return _FakeClientBundle()


class _FailingClientFactory:
    def get_client(self, website_id: UUID | str) -> _FakeClientBundle:
        del website_id
        raise RuntimeError("missing service account config")


@pytest.mark.asyncio
async def test_batch_processor_skips_already_indexed_urls_without_manual_override(
    tmp_path: Path,
) -> None:
    database_url = (
        f"sqlite+aiosqlite:///{tmp_path / 'batch-processor-skip-indexed.sqlite'}"
    )
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

    @asynccontextmanager
    async def scoped_session() -> AsyncIterator[AsyncSession]:
        session = session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with scoped_session() as session:
        website = Website(domain="example.net", site_url="https://example.net/")
        session.add(website)
        await session.flush()

        indexed_url = URL(
            website_id=website.id,
            url="https://example.net/indexed",
            sitemap_priority=0.8,
            lastmod=datetime.now(UTC),
        )
        not_indexed_url = URL(
            website_id=website.id,
            url="https://example.net/not-indexed",
            sitemap_priority=0.7,
            lastmod=datetime.now(UTC),
        )
        force_resubmit_url = URL(
            website_id=website.id,
            url="https://example.net/force-resubmit",
            sitemap_priority=0.6,
            lastmod=datetime.now(UTC),
            manual_priority_override=1.0,
        )
        session.add_all([indexed_url, not_indexed_url, force_resubmit_url])
        await session.flush()

        session.add_all(
            [
                IndexStatus(
                    url_id=indexed_url.id,
                    coverage_state="Indexed",
                    verdict=IndexVerdict.PASS,
                    raw_response={"source": "test"},
                ),
                IndexStatus(
                    url_id=force_resubmit_url.id,
                    coverage_state="Indexed",
                    verdict=IndexVerdict.PASS,
                    raw_response={"source": "test"},
                ),
            ]
        )
        await session.flush()

        website_id = website.id
        url_ids = [indexed_url.id, not_indexed_url.id, force_resubmit_url.id]

    queue_service = PriorityQueueService(session_factory=scoped_session)
    await queue_service.enqueue_many(url_ids)

    class _TrackingIndexingClient(_FakeIndexingClient):
        def __init__(self) -> None:
            self.submitted_urls: list[str] = []

        async def batch_submit(
            self,
            urls: list[str] | tuple[str, ...],
            action: str = "URL_UPDATED",
        ) -> BatchSubmitResult:
            self.submitted_urls.extend(urls)
            return await super().batch_submit(urls, action)

    class _TrackingClientBundle:
        def __init__(self) -> None:
            self.indexing = _TrackingIndexingClient()
            self.search_console = _FakeInspectionClient()

    class _TrackingClientFactory:
        def __init__(self) -> None:
            self.bundle = _TrackingClientBundle()

        def get_client(self, website_id: UUID | str) -> _TrackingClientBundle:
            del website_id
            return self.bundle

    tracking_factory = _TrackingClientFactory()
    processor = BatchProcessorService(
        priority_queue=queue_service,
        client_factory=cast(Any, tracking_factory),
        rate_limiter=cast(Any, _FakeRateLimiter()),
        session_factory=scoped_session,
        dequeue_batch_size=3,
        submit_batch_size=3,
        inspection_batch_size=3,
    )

    result = await processor.process_batch(website_id, requested_urls=3)

    assert result.status == BatchProcessorStatus.COMPLETED
    assert result.dequeued_urls == 3
    assert result.submitted_urls == 2
    assert result.submission_success_count == 2
    assert result.submission_failure_count == 0
    assert result.requeued_urls == 0

    submitted_urls = tracking_factory.bundle.indexing.submitted_urls
    assert "https://example.net/indexed" not in submitted_urls
    assert "https://example.net/not-indexed" in submitted_urls
    assert "https://example.net/force-resubmit" in submitted_urls

    async with scoped_session() as session:
        submission_logs = (
            (
                await session.execute(
                    select(SubmissionLog).order_by(SubmissionLog.submitted_at.asc())
                )
            )
            .scalars()
            .all()
        )

    assert len(submission_logs) == 3
    assert (
        sum(1 for log in submission_logs if log.status == SubmissionStatus.SKIPPED) == 1
    )

    await engine.dispose()


@pytest.mark.asyncio
async def test_batch_processor_handles_partial_failures_and_requeues(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'batch-processor.sqlite'}"
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

    @asynccontextmanager
    async def scoped_session() -> AsyncIterator[AsyncSession]:
        session = session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with scoped_session() as session:
        website = Website(domain="example.com", site_url="https://example.com/")
        session.add(website)
        await session.flush()

        now = datetime.now(UTC)
        urls = [
            URL(
                website_id=website.id,
                url="https://example.com/success",
                sitemap_priority=0.9,
                lastmod=now - timedelta(days=1),
            ),
            URL(
                website_id=website.id,
                url="https://example.com/submit-fail",
                sitemap_priority=0.8,
                lastmod=now - timedelta(days=2),
            ),
            URL(
                website_id=website.id,
                url="https://example.com/inspect-fail",
                sitemap_priority=0.7,
                lastmod=now - timedelta(days=3),
            ),
        ]
        session.add_all(urls)
        await session.flush()
        website_id = website.id
        url_ids = [item.id for item in urls]

    queue_service = PriorityQueueService(session_factory=scoped_session)
    await queue_service.enqueue_many(url_ids)

    rate_limiter = _FakeRateLimiter()
    processor = BatchProcessorService(
        priority_queue=queue_service,
        client_factory=cast(Any, _FakeClientFactory()),
        rate_limiter=cast(Any, rate_limiter),
        session_factory=scoped_session,
        dequeue_batch_size=3,
        submit_batch_size=3,
        inspection_batch_size=2,
    )

    progress_updates: list[BatchProgressUpdate] = []

    async def capture_progress(update: BatchProgressUpdate) -> None:
        progress_updates.append(update)

    result = await processor.process_batch(
        website_id,
        requested_urls=3,
        action=SubmissionAction.URL_UPDATED,
        progress_callback=capture_progress,
    )

    assert result.status == BatchProcessorStatus.PARTIAL_FAILURE
    assert result.dequeued_urls == 3
    assert result.submitted_urls == 3
    assert result.submission_success_count == 2
    assert result.submission_failure_count == 1
    assert result.inspected_urls == 2
    assert result.inspection_success_count == 1
    assert result.inspection_failure_count == 1
    assert result.requeued_urls == 2
    assert [update.stage for update in progress_updates] == [
        "dequeue",
        "submit",
        "inspect",
        "completed",
    ]
    assert rate_limiter.acquired_api_types == [
        "indexing",
        "indexing",
        "indexing",
        "inspection",
        "inspection",
    ]

    async with scoped_session() as session:
        submission_logs = (
            (
                await session.execute(
                    select(SubmissionLog).order_by(SubmissionLog.submitted_at.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(submission_logs) == 3

        index_statuses = (
            (
                await session.execute(
                    select(IndexStatus).order_by(IndexStatus.checked_at.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(index_statuses) == 2

        persisted_urls = {
            row.url: row
            for row in (
                await session.execute(select(URL).where(URL.website_id == website_id))
            )
            .scalars()
            .all()
        }

    assert persisted_urls["https://example.com/success"].current_priority == 0.0
    assert persisted_urls["https://example.com/submit-fail"].current_priority > 0.0
    assert persisted_urls["https://example.com/inspect-fail"].current_priority > 0.0

    await engine.dispose()


@pytest.mark.asyncio
async def test_batch_processor_requeues_all_urls_when_client_init_fails(
    tmp_path: Path,
) -> None:
    database_url = (
        f"sqlite+aiosqlite:///{tmp_path / 'batch-processor-factory-failure.sqlite'}"
    )
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

    @asynccontextmanager
    async def scoped_session() -> AsyncIterator[AsyncSession]:
        session = session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with scoped_session() as session:
        website = Website(domain="example.org", site_url="https://example.org/")
        session.add(website)
        await session.flush()

        queued_url = URL(
            website_id=website.id,
            url="https://example.org/page",
            sitemap_priority=0.8,
            lastmod=datetime.now(UTC),
        )
        session.add(queued_url)
        await session.flush()
        website_id = website.id
        url_id = queued_url.id

    queue_service = PriorityQueueService(session_factory=scoped_session)
    await queue_service.enqueue(url_id)

    processor = BatchProcessorService(
        priority_queue=queue_service,
        client_factory=cast(Any, _FailingClientFactory()),
        rate_limiter=cast(Any, _FakeRateLimiter()),
        session_factory=scoped_session,
        dequeue_batch_size=1,
    )

    result = await processor.process_batch(website_id, requested_urls=1)

    assert result.status == BatchProcessorStatus.FAILED
    assert result.requeued_urls == 1
    assert result.submission_failure_count == 1
    assert result.outcomes[0].submission_error_code == "CLIENT_INIT_FAILED"

    async with scoped_session() as session:
        persisted_url = await session.get(URL, url_id)

    assert persisted_url is not None
    assert persisted_url.current_priority > 0.0

    await engine.dispose()
