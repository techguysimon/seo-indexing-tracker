"""Tests for scheduler processing pipeline jobs and overlap protection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.sqlite")
os.environ.setdefault("SECRET_KEY", "test-secret")

from seo_indexing_tracker.config import get_settings
from seo_indexing_tracker.database import initialize_database, session_scope
from seo_indexing_tracker.models import URL, URLIndexStatus, Website
from seo_indexing_tracker.services.google_url_inspection_client import (
    IndexStatusResult,
    InspectionSystemStatus,
)
from seo_indexing_tracker.services.processing_pipeline import (
    INDEX_VERIFICATION_JOB_ID,
    SITEMAP_REFRESH_JOB_ID,
    URL_SUBMISSION_JOB_ID,
    SchedulerProcessingPipelineService,
    set_scheduler_processing_pipeline_service,
)
from seo_indexing_tracker.services.rate_limiter import RateLimitTimeoutError
from seo_indexing_tracker.services.scheduler import SchedulerService


@pytest.mark.asyncio
async def test_processing_pipeline_registers_scheduler_jobs(tmp_path: Path) -> None:
    scheduler = SchedulerService(
        enabled=True,
        jobstore_url=f"sqlite:///{tmp_path / 'pipeline-jobs.sqlite'}",
    )
    pipeline_service = SchedulerProcessingPipelineService(
        scheduler=scheduler,
        settings=get_settings(),
    )
    set_scheduler_processing_pipeline_service(pipeline_service)

    pipeline_service.register_jobs()
    await scheduler.start()
    try:
        jobs = scheduler.list_jobs()
        assert {job.job_id for job in jobs} == {
            URL_SUBMISSION_JOB_ID,
            INDEX_VERIFICATION_JOB_ID,
            SITEMAP_REFRESH_JOB_ID,
        }
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_processing_pipeline_skips_overlapping_job_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = SchedulerService(
        enabled=True,
        jobstore_url=f"sqlite:///{tmp_path / 'pipeline-overlap.sqlite'}",
    )
    pipeline_service = SchedulerProcessingPipelineService(
        scheduler=scheduler,
        settings=get_settings(),
    )
    set_scheduler_processing_pipeline_service(pipeline_service)
    pipeline_service.register_jobs()
    await scheduler.start()

    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_submit_urls() -> dict[str, int]:
        started.set()
        await release.wait()
        return {"processed_websites": 0, "dequeued_urls": 0, "failed_urls": 0}

    monkeypatch.setattr(pipeline_service, "_submit_urls", fake_submit_urls)

    try:
        first_run = asyncio.create_task(pipeline_service.run_url_submission_job())
        await started.wait()

        await pipeline_service.run_url_submission_job()
        release.set()
        await first_run
    finally:
        await scheduler.shutdown()

    metrics_by_job = {
        metrics.job_id: metrics for metrics in pipeline_service.monitoring_snapshot()
    }
    submission_metrics = metrics_by_job[URL_SUBMISSION_JOB_ID]

    assert submission_metrics.total_runs == 1
    assert submission_metrics.successful_runs == 1
    assert submission_metrics.failed_runs == 0
    assert submission_metrics.overlap_skips == 1


@pytest.mark.asyncio
async def test_processing_pipeline_verification_acquire_timeout_is_bounded(
    tmp_path: Path,
) -> None:
    scheduler = SchedulerService(
        enabled=False,
        jobstore_url=f"sqlite:///{tmp_path / 'pipeline-verify-acquire-timeout.sqlite'}",
    )

    class _TimeoutRateLimiter:
        def __init__(self) -> None:
            self.timeout_seconds_seen: float | None = None

        async def acquire(
            self,
            website_id: object,
            *,
            api_type: str,
            timeout_seconds: float | None = None,
        ) -> object:
            del website_id, api_type
            self.timeout_seconds_seen = timeout_seconds
            raise RateLimitTimeoutError("acquire timed out")

    class _InspectionClientShouldNotRun:
        async def inspect_url(self, url: str, site_url: str) -> object:
            raise AssertionError(f"inspect_url should not run for {url} {site_url}")

    rate_limiter = _TimeoutRateLimiter()
    pipeline_service = SchedulerProcessingPipelineService(
        scheduler=scheduler,
        settings=cast(
            Any,
            SimpleNamespace(
                SCHEDULER_URL_SUBMISSION_INTERVAL_SECONDS=300,
                SCHEDULER_INDEX_VERIFICATION_INTERVAL_SECONDS=900,
                SCHEDULER_SITEMAP_REFRESH_INTERVAL_SECONDS=3600,
                SCHEDULER_URL_SUBMISSION_BATCH_SIZE=100,
                SCHEDULER_INDEX_VERIFICATION_BATCH_SIZE=100,
            ),
        ),
        rate_limiter=cast(Any, rate_limiter),
    )

    candidate = SimpleNamespace(
        url_id=uuid4(),
        url="https://example.org/verify-timeout",
        site_url="https://example.org/",
    )

    result = await asyncio.wait_for(
        pipeline_service._inspect_single_url(
            website_id=uuid4(),
            candidate=cast(Any, candidate),
            client=cast(Any, _InspectionClientShouldNotRun()),
        ),
        timeout=1.0,
    )

    assert result.success is False
    assert result.error_code == "RATE_LIMITED"
    assert rate_limiter.timeout_seconds_seen == 10.0


@pytest.mark.asyncio
async def test_processing_pipeline_verification_non_rate_limit_acquire_error_does_not_set_429(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = SchedulerService(
        enabled=False,
        jobstore_url=f"sqlite:///{tmp_path / 'pipeline-verify-acquire-non-rate-limit.sqlite'}",
    )
    pipeline_service = SchedulerProcessingPipelineService(
        scheduler=scheduler,
        settings=cast(
            Any,
            SimpleNamespace(
                SCHEDULER_URL_SUBMISSION_INTERVAL_SECONDS=300,
                SCHEDULER_INDEX_VERIFICATION_INTERVAL_SECONDS=900,
                SCHEDULER_SITEMAP_REFRESH_INTERVAL_SECONDS=3600,
                SCHEDULER_URL_SUBMISSION_BATCH_SIZE=100,
                SCHEDULER_INDEX_VERIFICATION_BATCH_SIZE=10,
                QUOTA_RATE_LIMIT_COOLDOWN_SECONDS=3600,
            ),
        ),
    )

    await initialize_database()

    website_id = uuid4()
    candidate_url_id = uuid4()
    unique_domain = f"non-rate-limit-{website_id}.example"
    candidate_url = f"https://{unique_domain}/inspect"
    site_url = f"https://{unique_domain}/"

    async with session_scope() as session:
        session.add(
            Website(
                id=website_id,
                domain=unique_domain,
                site_url=site_url,
                is_active=True,
                quota_last_429_at=None,
            )
        )
        session.add(
            URL(
                id=candidate_url_id,
                website_id=website_id,
                url=candidate_url,
                latest_index_status=URLIndexStatus.UNCHECKED,
            )
        )

    website_credentials = SimpleNamespace(
        id=website_id,
        domain=unique_domain,
        service_account=SimpleNamespace(credentials_path="/tmp/non-rate-limit.json"),
        quota_last_429_at=None,
        internal_rate_limit_at=None,
    )

    async def fake_list_websites(*, requires_queued_urls: bool) -> list[Any]:
        assert requires_queued_urls is False
        return [website_credentials]

    async def fake_pick_urls(website_id: Any) -> list[Any]:
        del website_id
        return [
            SimpleNamespace(
                url_id=candidate_url_id,
                url=candidate_url,
                site_url=site_url,
            )
        ]

    async def fake_inspect_single_url(**_: Any) -> IndexStatusResult:
        return IndexStatusResult(
            inspection_url=candidate_url,
            site_url=site_url,
            success=False,
            http_status=None,
            system_status=InspectionSystemStatus.UNKNOWN,
            verdict=None,
            coverage_state=None,
            last_crawl_time=None,
            indexing_state=None,
            robots_txt_state=None,
            raw_response=None,
            error_code="RATE_LIMITER_ERROR",
            error_message="Failed to acquire inspection rate-limit permit",
            retry_after_seconds=None,
        )

    monkeypatch.setattr(
        pipeline_service,
        "_list_websites_with_credentials",
        fake_list_websites,
    )
    monkeypatch.setattr(
        pipeline_service,
        "_pick_urls_for_verification",
        fake_pick_urls,
    )
    monkeypatch.setattr(
        pipeline_service,
        "_inspect_single_url",
        fake_inspect_single_url,
    )
    monkeypatch.setattr(
        pipeline_service._client_factory,
        "register_website",
        lambda **_: None,
    )
    monkeypatch.setattr(
        pipeline_service._client_factory,
        "get_client",
        lambda _: SimpleNamespace(search_console=object()),
    )

    await pipeline_service._verify_index_statuses(execution_id=uuid4())

    async with session_scope() as session:
        website = await session.get(Website, website_id)

    assert website is not None
    assert website.quota_last_429_at is None


@pytest.mark.asyncio
async def test_processing_pipeline_submission_skips_websites_in_429_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = SchedulerService(
        enabled=False,
        jobstore_url=f"sqlite:///{tmp_path / 'pipeline-429-cooldown.sqlite'}",
    )
    pipeline_service = SchedulerProcessingPipelineService(
        scheduler=scheduler,
        settings=cast(
            Any,
            SimpleNamespace(
                SCHEDULER_URL_SUBMISSION_INTERVAL_SECONDS=300,
                SCHEDULER_INDEX_VERIFICATION_INTERVAL_SECONDS=900,
                SCHEDULER_SITEMAP_REFRESH_INTERVAL_SECONDS=3600,
                SCHEDULER_URL_SUBMISSION_BATCH_SIZE=100,
                SCHEDULER_INDEX_VERIFICATION_BATCH_SIZE=100,
                QUOTA_RATE_LIMIT_COOLDOWN_SECONDS=3600,
            ),
        ),
    )

    website_in_cooldown = SimpleNamespace(
        id=uuid4(),
        domain="in-cooldown.example",
        service_account=SimpleNamespace(credentials_path="/tmp/in-cooldown.json"),
        quota_last_429_at=datetime.now(UTC) - timedelta(minutes=10),
        internal_rate_limit_at=None,
    )
    website_ready = SimpleNamespace(
        id=uuid4(),
        domain="ready.example",
        service_account=SimpleNamespace(credentials_path="/tmp/ready.json"),
        quota_last_429_at=datetime.now(UTC) - timedelta(hours=2),
        internal_rate_limit_at=None,
    )

    async def fake_list_websites(*, requires_queued_urls: bool) -> list[Any]:
        assert requires_queued_urls is True
        return [website_in_cooldown, website_ready]

    processed_website_ids: list[str] = []

    async def fake_process_batch(
        website_id: Any,
        *,
        requested_urls: int,
        progress_callback: Any,
    ) -> Any:
        del requested_urls, progress_callback
        processed_website_ids.append(str(website_id))
        return SimpleNamespace(
            dequeued_urls=2,
            submission_success_count=2,
            submission_failure_count=0,
            submitted_urls=["https://example.com/a", "https://example.com/b"],
        )

    async def fake_log_activity(**_: Any) -> None:
        return

    async def fake_persist_checkpoint(**_: Any) -> None:
        return

    monkeypatch.setattr(
        pipeline_service,
        "_list_websites_with_credentials",
        fake_list_websites,
    )
    monkeypatch.setattr(
        pipeline_service._batch_processor,
        "process_batch",
        fake_process_batch,
    )
    monkeypatch.setattr(pipeline_service, "_log_activity", fake_log_activity)
    monkeypatch.setattr(
        pipeline_service._runner,
        "persist_checkpoint",
        fake_persist_checkpoint,
    )

    result = await pipeline_service._submit_urls(execution_id=uuid4())

    assert processed_website_ids == [str(website_ready.id)]
    assert result.summary == {
        "processed_websites": 1,
        "dequeued_urls": 2,
        "failed_urls": 0,
    }


@pytest.mark.asyncio
async def test_processing_pipeline_verification_skips_websites_in_429_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = SchedulerService(
        enabled=False,
        jobstore_url=f"sqlite:///{tmp_path / 'pipeline-verify-429-cooldown.sqlite'}",
    )
    pipeline_service = SchedulerProcessingPipelineService(
        scheduler=scheduler,
        settings=cast(
            Any,
            SimpleNamespace(
                SCHEDULER_URL_SUBMISSION_INTERVAL_SECONDS=300,
                SCHEDULER_INDEX_VERIFICATION_INTERVAL_SECONDS=900,
                SCHEDULER_SITEMAP_REFRESH_INTERVAL_SECONDS=3600,
                SCHEDULER_URL_SUBMISSION_BATCH_SIZE=100,
                SCHEDULER_INDEX_VERIFICATION_BATCH_SIZE=100,
                QUOTA_RATE_LIMIT_COOLDOWN_SECONDS=3600,
            ),
        ),
    )

    website_in_cooldown = SimpleNamespace(
        id=uuid4(),
        domain="in-cooldown.example",
        service_account=SimpleNamespace(credentials_path="/tmp/in-cooldown.json"),
        quota_last_429_at=datetime.now(UTC) - timedelta(minutes=10),
        internal_rate_limit_at=None,
    )
    website_ready = SimpleNamespace(
        id=uuid4(),
        domain="ready.example",
        service_account=SimpleNamespace(credentials_path="/tmp/ready.json"),
        quota_last_429_at=datetime.now(UTC) - timedelta(hours=2),
        internal_rate_limit_at=None,
    )

    async def fake_list_websites(*, requires_queued_urls: bool) -> list[Any]:
        assert requires_queued_urls is False
        return [website_in_cooldown, website_ready]

    picked_website_ids: list[str] = []

    async def fake_pick_urls(website_id: Any) -> list[Any]:
        picked_website_ids.append(str(website_id))
        return []

    monkeypatch.setattr(
        pipeline_service,
        "_list_websites_with_credentials",
        fake_list_websites,
    )
    monkeypatch.setattr(
        pipeline_service,
        "_pick_urls_for_verification",
        fake_pick_urls,
    )

    result = await pipeline_service._verify_index_statuses(execution_id=uuid4())

    assert picked_website_ids == [str(website_ready.id)]
    assert result.summary == {
        "processed_websites": 2,
        "inspected_urls": 0,
        "failed_urls": 0,
    }


def test_submission_cooldown_window_allows_resume_after_cooldown(
    tmp_path: Path,
) -> None:
    scheduler = SchedulerService(
        enabled=False,
        jobstore_url=f"sqlite:///{tmp_path / 'pipeline-cooldown-window.sqlite'}",
    )
    pipeline_service = SchedulerProcessingPipelineService(
        scheduler=scheduler,
        settings=cast(
            Any,
            SimpleNamespace(
                SCHEDULER_URL_SUBMISSION_INTERVAL_SECONDS=300,
                SCHEDULER_INDEX_VERIFICATION_INTERVAL_SECONDS=900,
                SCHEDULER_SITEMAP_REFRESH_INTERVAL_SECONDS=3600,
                SCHEDULER_URL_SUBMISSION_BATCH_SIZE=100,
                SCHEDULER_INDEX_VERIFICATION_BATCH_SIZE=100,
                QUOTA_RATE_LIMIT_COOLDOWN_SECONDS=3600,
            ),
        ),
    )
    cooldown_service = pipeline_service._cooldown_service

    website_ready = SimpleNamespace(
        id=uuid4(),
        domain="resume.example",
        quota_last_429_at=datetime.now(UTC) - timedelta(seconds=3600),
        internal_rate_limit_at=None,
    )
    website_in_cooldown = SimpleNamespace(
        id=uuid4(),
        domain="still-cooling.example",
        quota_last_429_at=datetime.now(UTC) - timedelta(seconds=3599),
        internal_rate_limit_at=None,
    )

    assert cooldown_service.get_cooldown_window(cast(Any, website_ready)) is None

    cooldown_window = cooldown_service.get_cooldown_window(
        cast(Any, website_in_cooldown)
    )
    assert cooldown_window is not None
    assert cooldown_window.domain == "still-cooling.example"


@pytest.mark.asyncio
async def test_pick_urls_for_verification_gates_recent_indexed_urls(
    tmp_path: Path,
) -> None:
    scheduler = SchedulerService(
        enabled=False,
        jobstore_url=f"sqlite:///{tmp_path / 'pipeline-verify-gate.sqlite'}",
    )
    pipeline_service = SchedulerProcessingPipelineService(
        scheduler=scheduler,
        settings=cast(
            Any,
            SimpleNamespace(
                SCHEDULER_URL_SUBMISSION_INTERVAL_SECONDS=300,
                SCHEDULER_INDEX_VERIFICATION_INTERVAL_SECONDS=900,
                SCHEDULER_SITEMAP_REFRESH_INTERVAL_SECONDS=3600,
                SCHEDULER_URL_SUBMISSION_BATCH_SIZE=100,
                SCHEDULER_INDEX_VERIFICATION_BATCH_SIZE=10,
                SCHEDULER_INDEXED_REVERIFICATION_MIN_AGE_SECONDS=7 * 24 * 60 * 60,
            ),
        ),
    )

    await initialize_database()
    website_id = uuid4()
    recent_checked_at = datetime.now(UTC) - timedelta(days=1)
    stale_checked_at = datetime.now(UTC) - timedelta(days=8)

    async with session_scope() as session:
        session.add(
            Website(
                id=website_id,
                domain=f"verify-gate-{website_id}.example",
                site_url="https://verify-gate.example",
                is_active=True,
            )
        )
        session.add_all(
            [
                URL(
                    website_id=website_id,
                    url="https://verify-gate.example/error",
                    latest_index_status=URLIndexStatus.ERROR,
                    last_checked_at=datetime.now(UTC),
                ),
                URL(
                    website_id=website_id,
                    url="https://verify-gate.example/not-indexed",
                    latest_index_status=URLIndexStatus.NOT_INDEXED,
                    last_checked_at=datetime.now(UTC),
                ),
                URL(
                    website_id=website_id,
                    url="https://verify-gate.example/unchecked",
                    latest_index_status=URLIndexStatus.UNCHECKED,
                    last_checked_at=None,
                ),
                URL(
                    website_id=website_id,
                    url="https://verify-gate.example/indexed-stale",
                    latest_index_status=URLIndexStatus.INDEXED,
                    last_checked_at=stale_checked_at,
                ),
                URL(
                    website_id=website_id,
                    url="https://verify-gate.example/indexed-recent",
                    latest_index_status=URLIndexStatus.INDEXED,
                    last_checked_at=recent_checked_at,
                ),
            ]
        )

    candidates = await pipeline_service._pick_urls_for_verification(website_id)
    candidate_urls = [candidate.url for candidate in candidates]

    assert "https://verify-gate.example/indexed-recent" not in candidate_urls
    assert "https://verify-gate.example/indexed-stale" in candidate_urls
    assert "https://verify-gate.example/not-indexed" in candidate_urls
    assert "https://verify-gate.example/error" in candidate_urls
    assert "https://verify-gate.example/unchecked" in candidate_urls


@pytest.mark.asyncio
async def test_pick_urls_for_verification_prioritizes_non_indexed_states(
    tmp_path: Path,
) -> None:
    scheduler = SchedulerService(
        enabled=False,
        jobstore_url=f"sqlite:///{tmp_path / 'pipeline-verify-priority.sqlite'}",
    )
    pipeline_service = SchedulerProcessingPipelineService(
        scheduler=scheduler,
        settings=cast(
            Any,
            SimpleNamespace(
                SCHEDULER_URL_SUBMISSION_INTERVAL_SECONDS=300,
                SCHEDULER_INDEX_VERIFICATION_INTERVAL_SECONDS=900,
                SCHEDULER_SITEMAP_REFRESH_INTERVAL_SECONDS=3600,
                SCHEDULER_URL_SUBMISSION_BATCH_SIZE=100,
                SCHEDULER_INDEX_VERIFICATION_BATCH_SIZE=3,
                SCHEDULER_INDEXED_REVERIFICATION_MIN_AGE_SECONDS=7 * 24 * 60 * 60,
            ),
        ),
    )

    await initialize_database()
    website_id = uuid4()
    stale_checked_at = datetime.now(UTC) - timedelta(days=9)

    async with session_scope() as session:
        session.add(
            Website(
                id=website_id,
                domain=f"verify-priority-{website_id}.example",
                site_url="https://verify-priority.example",
                is_active=True,
            )
        )
        session.add_all(
            [
                URL(
                    website_id=website_id,
                    url="https://verify-priority.example/error",
                    latest_index_status=URLIndexStatus.ERROR,
                    last_checked_at=datetime.now(UTC),
                ),
                URL(
                    website_id=website_id,
                    url="https://verify-priority.example/not-indexed",
                    latest_index_status=URLIndexStatus.NOT_INDEXED,
                    last_checked_at=datetime.now(UTC),
                ),
                URL(
                    website_id=website_id,
                    url="https://verify-priority.example/unchecked",
                    latest_index_status=URLIndexStatus.UNCHECKED,
                    last_checked_at=None,
                ),
                URL(
                    website_id=website_id,
                    url="https://verify-priority.example/indexed-stale",
                    latest_index_status=URLIndexStatus.INDEXED,
                    last_checked_at=stale_checked_at,
                ),
            ]
        )

    candidates = await pipeline_service._pick_urls_for_verification(website_id)

    assert [candidate.url for candidate in candidates] == [
        "https://verify-priority.example/error",
        "https://verify-priority.example/not-indexed",
        "https://verify-priority.example/unchecked",
    ]


@pytest.mark.asyncio
async def test_processing_pipeline_verification_google_429_sets_quota_last_429_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify actual Google HTTP 429 responses set quota_last_429_at for cooldown."""
    scheduler = SchedulerService(
        enabled=False,
        jobstore_url=f"sqlite:///{tmp_path / 'pipeline-google-429.sqlite'}",
    )
    pipeline_service = SchedulerProcessingPipelineService(
        scheduler=scheduler,
        settings=cast(
            Any,
            SimpleNamespace(
                SCHEDULER_URL_SUBMISSION_INTERVAL_SECONDS=300,
                SCHEDULER_INDEX_VERIFICATION_INTERVAL_SECONDS=900,
                SCHEDULER_SITEMAP_REFRESH_INTERVAL_SECONDS=3600,
                SCHEDULER_URL_SUBMISSION_BATCH_SIZE=100,
                SCHEDULER_INDEX_VERIFICATION_BATCH_SIZE=10,
                QUOTA_RATE_LIMIT_COOLDOWN_SECONDS=3600,
            ),
        ),
    )

    await initialize_database()

    website_id = uuid4()
    candidate_url_id = uuid4()
    unique_domain = f"google-429-{website_id}.example"
    candidate_url = f"https://{unique_domain}/inspect"
    site_url = f"https://{unique_domain}/"

    async with session_scope() as session:
        session.add(
            Website(
                id=website_id,
                domain=unique_domain,
                site_url=site_url,
                is_active=True,
                quota_last_429_at=None,
                internal_rate_limit_at=None,
            )
        )
        session.add(
            URL(
                id=candidate_url_id,
                website_id=website_id,
                url=candidate_url,
                latest_index_status=URLIndexStatus.UNCHECKED,
            )
        )

    website_credentials = SimpleNamespace(
        id=website_id,
        domain=unique_domain,
        service_account=SimpleNamespace(credentials_path="/tmp/google-429.json"),
        quota_last_429_at=None,
        internal_rate_limit_at=None,
    )

    async def fake_list_websites(*, requires_queued_urls: bool) -> list[Any]:
        assert requires_queued_urls is False
        return [website_credentials]

    async def fake_pick_urls(website_id: Any) -> list[Any]:
        del website_id
        return [
            SimpleNamespace(
                url_id=candidate_url_id,
                url=candidate_url,
                site_url=site_url,
            )
        ]

    async def fake_inspect_single_url(**_: Any) -> IndexStatusResult:
        # Actual Google HTTP 429 response
        return IndexStatusResult(
            inspection_url=candidate_url,
            site_url=site_url,
            success=False,
            http_status=429,  # This is what makes it a Google 429
            system_status=InspectionSystemStatus.UNKNOWN,
            verdict=None,
            coverage_state=None,
            last_crawl_time=None,
            indexing_state=None,
            robots_txt_state=None,
            raw_response=None,
            error_code="QUOTA_EXCEEDED",
            error_message="Google quota exceeded",
            retry_after_seconds=None,
        )

    monkeypatch.setattr(
        pipeline_service,
        "_list_websites_with_credentials",
        fake_list_websites,
    )
    monkeypatch.setattr(
        pipeline_service,
        "_pick_urls_for_verification",
        fake_pick_urls,
    )
    monkeypatch.setattr(
        pipeline_service,
        "_inspect_single_url",
        fake_inspect_single_url,
    )
    monkeypatch.setattr(
        pipeline_service._client_factory,
        "register_website",
        lambda **_: None,
    )
    monkeypatch.setattr(
        pipeline_service._client_factory,
        "get_client",
        lambda _: SimpleNamespace(search_console=object()),
    )

    await pipeline_service._verify_index_statuses(execution_id=uuid4())

    async with session_scope() as session:
        website = await session.get(Website, website_id)

    assert website is not None
    # Google 429 should set quota_last_429_at (triggers cooldown)
    assert website.quota_last_429_at is not None
    # Internal rate limit should NOT be set for actual Google 429
    assert website.internal_rate_limit_at is None


@pytest.mark.asyncio
async def test_processing_pipeline_verification_internal_rate_limit_sets_internal_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify internal rate limiting sets internal_rate_limit_at, not quota_last_429_at."""
    scheduler = SchedulerService(
        enabled=False,
        jobstore_url=f"sqlite:///{tmp_path / 'pipeline-internal-rate-limit.sqlite'}",
    )
    pipeline_service = SchedulerProcessingPipelineService(
        scheduler=scheduler,
        settings=cast(
            Any,
            SimpleNamespace(
                SCHEDULER_URL_SUBMISSION_INTERVAL_SECONDS=300,
                SCHEDULER_INDEX_VERIFICATION_INTERVAL_SECONDS=900,
                SCHEDULER_SITEMAP_REFRESH_INTERVAL_SECONDS=3600,
                SCHEDULER_URL_SUBMISSION_BATCH_SIZE=100,
                SCHEDULER_INDEX_VERIFICATION_BATCH_SIZE=10,
                QUOTA_RATE_LIMIT_COOLDOWN_SECONDS=3600,
            ),
        ),
    )

    await initialize_database()

    website_id = uuid4()
    candidate_url_id = uuid4()
    unique_domain = f"internal-rl-{website_id}.example"
    candidate_url = f"https://{unique_domain}/inspect"
    site_url = f"https://{unique_domain}/"

    async with session_scope() as session:
        session.add(
            Website(
                id=website_id,
                domain=unique_domain,
                site_url=site_url,
                is_active=True,
                quota_last_429_at=None,
                internal_rate_limit_at=None,
            )
        )
        session.add(
            URL(
                id=candidate_url_id,
                website_id=website_id,
                url=candidate_url,
                latest_index_status=URLIndexStatus.UNCHECKED,
            )
        )

    website_credentials = SimpleNamespace(
        id=website_id,
        domain=unique_domain,
        service_account=SimpleNamespace(credentials_path="/tmp/internal-rl.json"),
        quota_last_429_at=None,
        internal_rate_limit_at=None,
    )

    async def fake_list_websites(*, requires_queued_urls: bool) -> list[Any]:
        assert requires_queued_urls is False
        return [website_credentials]

    async def fake_pick_urls(website_id: Any) -> list[Any]:
        del website_id
        return [
            SimpleNamespace(
                url_id=candidate_url_id,
                url=candidate_url,
                site_url=site_url,
            )
        ]

    async def fake_inspect_single_url(**_: Any) -> IndexStatusResult:
        # Internal rate limiter error (no HTTP status)
        return IndexStatusResult(
            inspection_url=candidate_url,
            site_url=site_url,
            success=False,
            http_status=None,  # No HTTP status = internal rate limit
            system_status=InspectionSystemStatus.UNKNOWN,
            verdict=None,
            coverage_state=None,
            last_crawl_time=None,
            indexing_state=None,
            robots_txt_state=None,
            raw_response=None,
            error_code="RATE_LIMITED",
            error_message="Internal rate limiter exhausted",
            retry_after_seconds=None,
        )

    monkeypatch.setattr(
        pipeline_service,
        "_list_websites_with_credentials",
        fake_list_websites,
    )
    monkeypatch.setattr(
        pipeline_service,
        "_pick_urls_for_verification",
        fake_pick_urls,
    )
    monkeypatch.setattr(
        pipeline_service,
        "_inspect_single_url",
        fake_inspect_single_url,
    )
    monkeypatch.setattr(
        pipeline_service._client_factory,
        "register_website",
        lambda **_: None,
    )
    monkeypatch.setattr(
        pipeline_service._client_factory,
        "get_client",
        lambda _: SimpleNamespace(search_console=object()),
    )

    await pipeline_service._verify_index_statuses(execution_id=uuid4())

    async with session_scope() as session:
        website = await session.get(Website, website_id)

    assert website is not None
    # Internal rate limit should set internal_rate_limit_at
    assert website.internal_rate_limit_at is not None
    # Should NOT set quota_last_429_at (that's only for actual Google 429s)
    assert website.quota_last_429_at is None


@pytest.mark.asyncio
async def test_processing_pipeline_cooldown_only_checks_quota_last_429_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify cooldown only triggers on quota_last_429_at, not internal_rate_limit_at."""
    scheduler = SchedulerService(
        enabled=False,
        jobstore_url=f"sqlite:///{tmp_path / 'pipeline-cooldown-check.sqlite'}",
    )
    pipeline_service = SchedulerProcessingPipelineService(
        scheduler=scheduler,
        settings=cast(
            Any,
            SimpleNamespace(
                SCHEDULER_URL_SUBMISSION_INTERVAL_SECONDS=300,
                SCHEDULER_INDEX_VERIFICATION_INTERVAL_SECONDS=900,
                SCHEDULER_SITEMAP_REFRESH_INTERVAL_SECONDS=3600,
                SCHEDULER_URL_SUBMISSION_BATCH_SIZE=100,
                SCHEDULER_INDEX_VERIFICATION_BATCH_SIZE=100,
                QUOTA_RATE_LIMIT_COOLDOWN_SECONDS=3600,
            ),
        ),
    )

    # Website with recent internal rate limit but NO Google 429
    website_internal_only = SimpleNamespace(
        id=uuid4(),
        domain="internal-only.example",
        service_account=SimpleNamespace(credentials_path="/tmp/internal-only.json"),
        quota_last_429_at=None,  # No Google 429
        internal_rate_limit_at=None,  # Also no internal rate limit
    )

    # Website with recent Google 429
    website_google_429 = SimpleNamespace(
        id=uuid4(),
        domain="google-429-cooldown.example",
        service_account=SimpleNamespace(
            credentials_path="/tmp/google-429-cooldown.json"
        ),
        quota_last_429_at=datetime.now(UTC) - timedelta(minutes=10),
        internal_rate_limit_at=None,
    )

    async def fake_list_websites(*, requires_queued_urls: bool) -> list[Any]:
        assert requires_queued_urls is True
        return [website_internal_only, website_google_429]

    processed_website_ids: list[str] = []

    async def fake_process_batch(
        website_id: Any,
        *,
        requested_urls: int,
        progress_callback: Any,
    ) -> Any:
        del requested_urls, progress_callback
        processed_website_ids.append(str(website_id))
        return SimpleNamespace(
            dequeued_urls=2,
            submission_success_count=2,
            submission_failure_count=0,
            submitted_urls=["https://example.com/a", "https://example.com/b"],
        )

    async def fake_log_activity(**_: Any) -> None:
        return

    async def fake_persist_checkpoint(**_: Any) -> None:
        return

    monkeypatch.setattr(
        pipeline_service,
        "_list_websites_with_credentials",
        fake_list_websites,
    )
    monkeypatch.setattr(
        pipeline_service._batch_processor,
        "process_batch",
        fake_process_batch,
    )
    monkeypatch.setattr(pipeline_service, "_log_activity", fake_log_activity)
    monkeypatch.setattr(
        pipeline_service._runner,
        "persist_checkpoint",
        fake_persist_checkpoint,
    )

    result = await pipeline_service._submit_urls(execution_id=uuid4())

    # Only the website with NO quota_last_429_at should be processed
    # The website with Google 429 should be in cooldown
    assert processed_website_ids == [str(website_internal_only.id)]
    assert result.summary == {
        "processed_websites": 1,
        "dequeued_urls": 2,
        "failed_urls": 0,
    }


@pytest.mark.asyncio
async def test_processing_pipeline_internal_rate_limit_triggers_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that internal_rate_limit_at triggers a cooldown (not quota_last_429_at)."""
    scheduler = SchedulerService(
        enabled=False,
        jobstore_url=f"sqlite:///{tmp_path / 'pipeline-internal-rl-cooldown.sqlite'}",
    )
    pipeline_service = SchedulerProcessingPipelineService(
        scheduler=scheduler,
        settings=cast(
            Any,
            SimpleNamespace(
                SCHEDULER_URL_SUBMISSION_INTERVAL_SECONDS=300,
                SCHEDULER_INDEX_VERIFICATION_INTERVAL_SECONDS=900,
                SCHEDULER_SITEMAP_REFRESH_INTERVAL_SECONDS=3600,
                SCHEDULER_URL_SUBMISSION_BATCH_SIZE=100,
                SCHEDULER_INDEX_VERIFICATION_BATCH_SIZE=100,
                QUOTA_RATE_LIMIT_COOLDOWN_SECONDS=3600,
            ),
        ),
    )

    cooldown_service = pipeline_service._cooldown_service

    # Website with recent internal rate limit (should be in cooldown)
    website_internal_cooldown = SimpleNamespace(
        id=uuid4(),
        domain="internal-cooldown.example",
        service_account=SimpleNamespace(credentials_path="/tmp/internal-cooldown.json"),
        quota_last_429_at=None,  # No Google 429
        internal_rate_limit_at=datetime.now(UTC)
        - timedelta(minutes=10),  # Recent internal RL
    )

    # Website with no rate limit issues (should be processed)
    website_ready = SimpleNamespace(
        id=uuid4(),
        domain="ready-no-rl.example",
        service_account=SimpleNamespace(credentials_path="/tmp/ready-no-rl.json"),
        quota_last_429_at=None,
        internal_rate_limit_at=None,
    )

    # Verify cooldown window detection
    internal_cooldown_window = cooldown_service.get_cooldown_window(
        cast(Any, website_internal_cooldown)
    )
    assert internal_cooldown_window is not None
    assert internal_cooldown_window.is_internal_rate_limit is True

    ready_window = cooldown_service.get_cooldown_window(cast(Any, website_ready))
    assert ready_window is None

    async def fake_list_websites(*, requires_queued_urls: bool) -> list[Any]:
        assert requires_queued_urls is True
        return [website_internal_cooldown, website_ready]

    processed_website_ids: list[str] = []

    async def fake_process_batch(
        website_id: Any,
        *,
        requested_urls: int,
        progress_callback: Any,
    ) -> Any:
        del requested_urls, progress_callback
        processed_website_ids.append(str(website_id))
        return SimpleNamespace(
            dequeued_urls=2,
            submission_success_count=2,
            submission_failure_count=0,
            submitted_urls=["https://example.com/a", "https://example.com/b"],
        )

    logged_activities: list[dict[str, Any]] = []

    async def fake_log_activity(**kwargs: Any) -> None:
        logged_activities.append(kwargs)

    async def fake_persist_checkpoint(**_: Any) -> None:
        return

    monkeypatch.setattr(
        pipeline_service,
        "_list_websites_with_credentials",
        fake_list_websites,
    )
    monkeypatch.setattr(
        pipeline_service._batch_processor,
        "process_batch",
        fake_process_batch,
    )
    monkeypatch.setattr(pipeline_service, "_log_activity", fake_log_activity)
    monkeypatch.setattr(
        pipeline_service._runner,
        "persist_checkpoint",
        fake_persist_checkpoint,
    )

    result = await pipeline_service._submit_urls(execution_id=uuid4())

    # Only the website with NO internal_rate_limit_at should be processed
    # The website with internal rate limit should be in cooldown
    assert processed_website_ids == [str(website_ready.id)]
    assert result.summary == {
        "processed_websites": 1,
        "dequeued_urls": 2,
        "failed_urls": 0,
    }

    # Verify the activity log shows the correct cooldown type
    skipped_activities = [
        a
        for a in logged_activities
        if a.get("event_type") == "url_submission_skipped_rate_limited"
    ]
    assert len(skipped_activities) == 1
    assert skipped_activities[0]["metadata"]["cooldown_type"] == "internal_rate_limit"
    assert "internal_rate_limit_at" in skipped_activities[0]["metadata"]
    assert "quota_last_429_at" not in skipped_activities[0]["metadata"]
