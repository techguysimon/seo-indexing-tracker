"""Scheduled processing pipeline jobs for submission, verification, and refresh."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import func, insert, nullsfirst, select
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.config import Settings
from seo_indexing_tracker.database import session_scope
from seo_indexing_tracker.models import (
    JobExecution,
    IndexStatus,
    IndexVerdict,
    ServiceAccount,
    Sitemap,
    URL,
    Website,
)
from seo_indexing_tracker.services.activity_service import ActivityService
from seo_indexing_tracker.services.batch_processor import BatchProcessorService
from seo_indexing_tracker.services.google_api_factory import (
    WebsiteGoogleAPIClients,
    WebsiteServiceAccountConfig,
)
from seo_indexing_tracker.services.google_url_inspection_client import (
    IndexStatusResult,
    InspectionSystemStatus,
)
from seo_indexing_tracker.services.priority_queue import PriorityQueueService
from seo_indexing_tracker.services.quota_service import QuotaService
from seo_indexing_tracker.services.rate_limiter import (
    ConcurrentRequestLimitExceededError,
    RateLimitTimeoutError,
    RateLimitTokenUnavailableError,
    RateLimiterService,
)
from seo_indexing_tracker.services.scheduler import SchedulerService
from seo_indexing_tracker.services.url_discovery import URLDiscoveryService
from seo_indexing_tracker.utils.index_status import (
    derive_url_index_status_from_coverage_state,
)

_job_logger = logging.getLogger("seo_indexing_tracker.scheduler.jobs")

_pipeline_service: SchedulerProcessingPipelineService | None = None

URL_SUBMISSION_JOB_ID = "url-submission-job"
INDEX_VERIFICATION_JOB_ID = "index-verification-job"
SITEMAP_REFRESH_JOB_ID = "sitemap-refresh-job"


@dataclass(slots=True)
class JobExecutionMetrics:
    """In-memory runtime metrics for one scheduled job."""

    job_id: str
    name: str
    total_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    overlap_skips: int = 0
    running: bool = False
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_duration_ms: float | None = None
    last_error: str | None = None


@dataclass(slots=True, frozen=True)
class JobRunResult:
    """Normalized result metadata persisted to job execution history."""

    summary: dict[str, int]
    urls_processed: int
    checkpoint_data: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class _WebsiteCredentials:
    website_id: UUID
    credentials_path: str


@dataclass(slots=True, frozen=True)
class _VerificationCandidate:
    url_id: UUID
    url: str
    site_url: str


class _InspectionClient(Protocol):
    async def inspect_url(
        self,
        url: str,
        site_url: str,
        *,
        website_id: UUID | None = None,
        session: AsyncSession | None = None,
    ) -> IndexStatusResult: ...


class _MappedGoogleClientFactory:
    """Runtime credentials map used by batch and verification jobs."""

    def __init__(self) -> None:
        self._credentials_by_website: dict[str, str] = {}
        self._clients_by_website: dict[str, WebsiteGoogleAPIClients] = {}

    def register_website(self, *, website_id: UUID, credentials_path: str) -> None:
        website_key = str(website_id)
        known_credentials_path = self._credentials_by_website.get(website_key)
        if known_credentials_path == credentials_path:
            return

        self._credentials_by_website[website_key] = credentials_path
        self._clients_by_website.pop(website_key, None)

    def get_client(self, website_id: UUID | str) -> WebsiteGoogleAPIClients:
        website_key = str(website_id)
        known_client = self._clients_by_website.get(website_key)
        if known_client is not None:
            return known_client

        credentials_path = self._credentials_by_website.get(website_key)
        if credentials_path is None:
            raise RuntimeError(
                f"No service account credentials configured for website {website_key}"
            )

        client_bundle = WebsiteGoogleAPIClients(
            config=WebsiteServiceAccountConfig(credentials_path=credentials_path)
        )
        self._clients_by_website[website_key] = client_bundle
        return client_bundle


class _OverlapProtectedRunner:
    """Execute jobs with overlap protection and per-job metrics."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._metrics: dict[str, JobExecutionMetrics] = {}
        self._activity_service = ActivityService()

    def register(self, *, job_id: str, name: str) -> None:
        self._locks.setdefault(job_id, asyncio.Lock())
        self._metrics.setdefault(job_id, JobExecutionMetrics(job_id=job_id, name=name))

    def snapshot(self) -> list[JobExecutionMetrics]:
        return [
            JobExecutionMetrics(
                job_id=metrics.job_id,
                name=metrics.name,
                total_runs=metrics.total_runs,
                successful_runs=metrics.successful_runs,
                failed_runs=metrics.failed_runs,
                overlap_skips=metrics.overlap_skips,
                running=metrics.running,
                last_started_at=metrics.last_started_at,
                last_finished_at=metrics.last_finished_at,
                last_duration_ms=metrics.last_duration_ms,
                last_error=metrics.last_error,
            )
            for metrics in self._metrics.values()
        ]

    async def run(
        self,
        *,
        job_id: str,
        run: Callable[[UUID], Awaitable[JobRunResult]],
    ) -> None:
        lock = self._locks[job_id]
        metrics = self._metrics[job_id]
        if lock.locked():
            metrics.overlap_skips += 1
            _job_logger.warning(
                "scheduler_job_overlap_skipped", extra={"job_id": job_id}
            )
            return

        async with lock:
            metrics.total_runs += 1
            metrics.running = True
            metrics.last_started_at = datetime.now(UTC)
            started_at = perf_counter()
            execution_id = await self._start_job_execution(
                job_id=job_id, metrics=metrics
            )

            try:
                job_result = await self._invoke_job_run(
                    run=run,
                    execution_id=execution_id,
                )
                metrics.successful_runs += 1
                metrics.last_error = None
                _job_logger.info(
                    "scheduler_pipeline_job_completed",
                    extra={"job_id": job_id, **job_result.summary},
                )
                await self._finish_job_execution(
                    execution_id=execution_id,
                    status="success",
                    urls_processed=job_result.urls_processed,
                    checkpoint_data=job_result.checkpoint_data,
                    error_message=None,
                )
            except Exception as error:
                metrics.failed_runs += 1
                metrics.last_error = str(error)
                _job_logger.exception(
                    "scheduler_pipeline_job_failed",
                    extra={"job_id": job_id},
                )
                await self._finish_job_execution(
                    execution_id=execution_id,
                    status="failed",
                    urls_processed=None,
                    checkpoint_data=None,
                    error_message=str(error),
                )
            finally:
                metrics.running = False
                metrics.last_finished_at = datetime.now(UTC)
                metrics.last_duration_ms = round(
                    (perf_counter() - started_at) * 1000, 2
                )

    async def _start_job_execution(
        self,
        *,
        job_id: str,
        metrics: JobExecutionMetrics,
    ) -> UUID:
        execution = JobExecution(
            job_id=job_id,
            job_name=metrics.name,
            status="running",
            checkpoint_data={"stage": "started", "job_id": job_id},
        )
        async with session_scope() as session:
            session.add(execution)
            await session.flush()
            await self._activity_service.log_activity(
                session=session,
                event_type="job_started",
                website_id=None,
                resource_type="job",
                resource_id=execution.id,
                message=f"{metrics.name} started",
                metadata={"job_id": job_id, "job_execution_id": str(execution.id)},
            )
            return execution.id

    async def _finish_job_execution(
        self,
        *,
        execution_id: UUID,
        status: str,
        urls_processed: int | None,
        checkpoint_data: dict[str, Any] | None,
        error_message: str | None,
    ) -> None:
        finished_at = datetime.now(UTC)
        async with session_scope() as session:
            execution = await session.get(JobExecution, execution_id)
            if execution is None:
                return
            execution.status = status
            execution.finished_at = finished_at
            if urls_processed is not None:
                execution.urls_processed = urls_processed
            if checkpoint_data is not None:
                execution.checkpoint_data = checkpoint_data
            execution.error_message = error_message
            await session.flush()
            await self._activity_service.log_activity(
                session=session,
                event_type="job_completed",
                website_id=execution.website_id,
                resource_type="job",
                resource_id=execution.id,
                message=f"{execution.job_name} {status}",
                metadata={
                    "job_id": execution.job_id,
                    "job_execution_id": str(execution.id),
                    "status": status,
                    "urls_processed": execution.urls_processed,
                    "error_message": error_message,
                },
            )

    async def persist_checkpoint(
        self,
        *,
        execution_id: UUID,
        checkpoint_data: dict[str, Any],
        urls_processed: int,
    ) -> None:
        async with session_scope() as session:
            execution = await session.get(JobExecution, execution_id)
            if execution is None:
                return
            execution.checkpoint_data = checkpoint_data
            execution.urls_processed = urls_processed

    async def _invoke_job_run(
        self,
        *,
        run: Callable[[UUID], Awaitable[JobRunResult]],
        execution_id: UUID,
    ) -> JobRunResult:
        parameter_count: int | None = None
        try:
            parameter_count = len(inspect.signature(run).parameters)
        except (TypeError, ValueError):
            parameter_count = None

        if parameter_count == 0:
            async with asyncio.timeout(900):  # 15 minute timeout
                legacy_result = await cast(Any, run)()
            if isinstance(legacy_result, JobRunResult):
                return legacy_result
            if isinstance(legacy_result, dict):
                urls_processed = int(
                    legacy_result.get("dequeued_urls")
                    or legacy_result.get("inspected_urls")
                    or legacy_result.get("discovered_urls")
                    or 0
                )
                return JobRunResult(
                    summary=cast(dict[str, int], legacy_result),
                    urls_processed=urls_processed,
                )
            raise RuntimeError("Legacy scheduler job returned unexpected payload")

        async with asyncio.timeout(900):  # 15 minute timeout
            return await run(execution_id)


class SchedulerProcessingPipelineService:
    """Configure and run recurring scheduler jobs for processing pipeline work."""

    def __init__(
        self,
        *,
        scheduler: SchedulerService,
        settings: Settings,
        queue_service: PriorityQueueService | None = None,
        discovery_service: URLDiscoveryService | None = None,
        rate_limiter: RateLimiterService | None = None,
        client_factory: _MappedGoogleClientFactory | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._settings = settings
        self._queue_service = queue_service or PriorityQueueService()
        self._discovery_service = discovery_service or URLDiscoveryService()
        self._client_factory = client_factory or _MappedGoogleClientFactory()
        self._rate_limiter = rate_limiter or RateLimiterService(
            quota_service=QuotaService(settings=settings)
        )
        self._batch_processor = BatchProcessorService(
            priority_queue=self._queue_service,
            client_factory=cast(Any, self._client_factory),
            rate_limiter=self._rate_limiter,
        )
        self._activity_service = ActivityService()
        self._runner = _OverlapProtectedRunner()

    def register_jobs(self) -> None:
        if not self._scheduler.enabled:
            return

        self._runner.register(job_id=URL_SUBMISSION_JOB_ID, name="URL Submission Job")
        self._runner.register(
            job_id=INDEX_VERIFICATION_JOB_ID,
            name="Index Verification Job",
        )
        self._runner.register(job_id=SITEMAP_REFRESH_JOB_ID, name="Sitemap Refresh Job")

        self._scheduler.add_interval_job(
            job_id=URL_SUBMISSION_JOB_ID,
            func=run_scheduled_url_submission_job,
            seconds=self._settings.SCHEDULER_URL_SUBMISSION_INTERVAL_SECONDS,
            name="Scheduled URL submission",
        )
        self._scheduler.add_interval_job(
            job_id=INDEX_VERIFICATION_JOB_ID,
            func=run_scheduled_index_verification_job,
            seconds=self._settings.SCHEDULER_INDEX_VERIFICATION_INTERVAL_SECONDS,
            name="Scheduled index verification",
        )
        self._scheduler.add_interval_job(
            job_id=SITEMAP_REFRESH_JOB_ID,
            func=run_scheduled_sitemap_refresh_job,
            seconds=self._settings.SCHEDULER_SITEMAP_REFRESH_INTERVAL_SECONDS,
            name="Scheduled sitemap refresh",
        )

    def monitoring_snapshot(self) -> list[JobExecutionMetrics]:
        return self._runner.snapshot()

    async def run_url_submission_job(self) -> None:
        await self._runner.run(job_id=URL_SUBMISSION_JOB_ID, run=self._submit_urls)

    async def run_index_verification_job(self) -> None:
        await self._runner.run(
            job_id=INDEX_VERIFICATION_JOB_ID,
            run=self._verify_index_statuses,
        )

    async def run_sitemap_refresh_job(self) -> None:
        await self._runner.run(
            job_id=SITEMAP_REFRESH_JOB_ID,
            run=self._refresh_sitemaps,
        )

    async def _submit_urls(self, execution_id: UUID) -> JobRunResult:
        website_credentials = await self._list_websites_with_credentials(
            requires_queued_urls=True
        )
        _job_logger.debug(
            "submit_urls_websites_found",
            extra={
                "website_count": len(website_credentials),
                "website_ids": [str(w.website_id) for w in website_credentials],
            },
        )
        if not website_credentials:
            _job_logger.debug("submit_urls_no_websites_with_queued_urls")
        processed_websites = 0
        dequeued_urls = 0
        failed_urls = 0
        last_checkpoint_data: dict[str, Any] | None = {
            "job_id": URL_SUBMISSION_JOB_ID,
            "stage": "initialized",
            "processed_websites": 0,
            "urls_processed": 0,
        }

        async def persist_batch_checkpoint(
            website_id: UUID,
            checkpoint_data: dict[str, Any] | None,
        ) -> None:
            nonlocal last_checkpoint_data
            if checkpoint_data is None:
                return

            urls_processed = int(checkpoint_data.get("processed_urls", 0))
            payload = {
                "job_id": URL_SUBMISSION_JOB_ID,
                "stage": "submit",
                "website_id": str(website_id),
                "processed_websites": processed_websites,
                "urls_processed": dequeued_urls + urls_processed,
                "batch_checkpoint": checkpoint_data,
            }
            last_checkpoint_data = payload
            await self._runner.persist_checkpoint(
                execution_id=execution_id,
                checkpoint_data=payload,
                urls_processed=dequeued_urls + urls_processed,
            )

        for website in website_credentials:
            _job_logger.debug(
                "submit_urls_processing_website",
                extra={
                    "website_id": str(website.website_id),
                    "batch_size": self._settings.SCHEDULER_URL_SUBMISSION_BATCH_SIZE,
                },
            )
            self._client_factory.register_website(
                website_id=website.website_id,
                credentials_path=website.credentials_path,
            )

            async def progress_callback(update: Any) -> None:
                await persist_batch_checkpoint(
                    website_id=website.website_id,
                    checkpoint_data=getattr(update, "checkpoint_data", None),
                )

            result = await self._batch_processor.process_batch(
                website.website_id,
                requested_urls=self._settings.SCHEDULER_URL_SUBMISSION_BATCH_SIZE,
                progress_callback=progress_callback,
            )
            _job_logger.debug(
                "submit_urls_batch_complete",
                extra={
                    "website_id": str(website.website_id),
                    "dequeued_urls": result.dequeued_urls,
                    "submission_success_count": result.submission_success_count,
                    "submission_failure_count": result.submission_failure_count,
                },
            )
            processed_websites += 1
            dequeued_urls += result.dequeued_urls
            failed_urls += result.submission_failure_count
            await self._log_activity(
                event_type="url_submitted",
                website_id=website.website_id,
                resource_type="website",
                resource_id=website.website_id,
                message=(
                    f"Submitted {result.submission_success_count} URLs "
                    f"for website {website.website_id}"
                ),
                metadata={
                    "dequeued_urls": result.dequeued_urls,
                    "submitted_urls": result.submitted_urls,
                    "submission_failures": result.submission_failure_count,
                },
            )

            last_checkpoint_data = {
                "job_id": URL_SUBMISSION_JOB_ID,
                "stage": "submit",
                "website_id": str(website.website_id),
                "processed_websites": processed_websites,
                "urls_processed": dequeued_urls,
            }
            await self._runner.persist_checkpoint(
                execution_id=execution_id,
                checkpoint_data=last_checkpoint_data,
                urls_processed=dequeued_urls,
            )

        summary = {
            "processed_websites": processed_websites,
            "dequeued_urls": dequeued_urls,
            "failed_urls": failed_urls,
        }
        return JobRunResult(
            summary=summary,
            urls_processed=dequeued_urls,
            checkpoint_data=last_checkpoint_data,
        )

    async def _verify_index_statuses(self, execution_id: UUID) -> JobRunResult:
        del execution_id
        website_credentials = await self._list_websites_with_credentials(
            requires_queued_urls=False
        )
        _job_logger.debug(
            "verify_index_websites_found",
            extra={
                "website_count": len(website_credentials),
                "website_ids": [str(w.website_id) for w in website_credentials],
            },
        )
        if not website_credentials:
            _job_logger.debug("verify_index_no_websites_with_credentials")
        inspected_urls = 0
        failed_urls = 0

        for website in website_credentials:
            _job_logger.debug(
                "verify_index_processing_website",
                extra={"website_id": str(website.website_id)},
            )
            self._client_factory.register_website(
                website_id=website.website_id,
                credentials_path=website.credentials_path,
            )
            candidate_urls = await self._pick_urls_for_verification(website.website_id)
            _job_logger.debug(
                "verify_index_candidates_found",
                extra={
                    "website_id": str(website.website_id),
                    "candidate_count": len(candidate_urls),
                },
            )
            if not candidate_urls:
                continue

            inspection_client = self._client_factory.get_client(
                website.website_id
            ).search_console
            inspection_rows: list[dict[str, object]] = []
            results_by_url_id: dict[UUID, IndexStatusResult] = {}
            for candidate in candidate_urls:
                result = await self._inspect_single_url(
                    website_id=website.website_id,
                    candidate=candidate,
                    client=inspection_client,
                )
                inspection_rows.append(
                    self._index_status_row(url_id=candidate.url_id, result=result)
                )
                results_by_url_id[candidate.url_id] = result
                inspected_urls += 1
                if not result.success:
                    failed_urls += 1

            async with session_scope() as session:
                await session.execute(insert(IndexStatus), inspection_rows)

                url_ids = list(results_by_url_id.keys())
                urls = await session.scalars(select(URL).where(URL.id.in_(url_ids)))
                url_by_id = {url.id: url for url in urls}

                checked_at = datetime.now(UTC)
                for url_id, result in results_by_url_id.items():
                    url = url_by_id.get(url_id)
                    if url is None:
                        continue
                    coverage_state = result.coverage_state or "INSPECTION_FAILED"
                    derived_status = derive_url_index_status_from_coverage_state(
                        coverage_state
                    )
                    url.latest_index_status = derived_status
                    url.last_checked_at = checked_at

        summary = {
            "processed_websites": len(website_credentials),
            "inspected_urls": inspected_urls,
            "failed_urls": failed_urls,
        }
        return JobRunResult(
            summary=summary,
            urls_processed=inspected_urls,
            checkpoint_data={
                "job_id": INDEX_VERIFICATION_JOB_ID,
                "processed_websites": len(website_credentials),
                "urls_processed": inspected_urls,
            },
        )

    async def _refresh_sitemaps(self, execution_id: UUID) -> JobRunResult:
        sitemap_ids = await self._list_active_sitemap_ids()
        refreshed_sitemaps = 0
        discovered_urls = 0
        requeued_urls = 0
        last_checkpoint_data: dict[str, Any] | None = {
            "job_id": SITEMAP_REFRESH_JOB_ID,
            "stage": "initialized",
            "processed_sitemaps": 0,
            "urls_processed": 0,
        }

        for sitemap_id in sitemap_ids:
            discovery_result = await self._discovery_service.discover_urls(sitemap_id)
            refreshed_sitemaps += 1
            discovered_urls += (
                discovery_result.new_count + discovery_result.modified_count
            )

            sitemap_url_ids = await self._list_sitemap_url_ids(sitemap_id)
            if not sitemap_url_ids:
                continue

            requeued_urls += await self._queue_service.enqueue_many(sitemap_url_ids)
            last_checkpoint_data = {
                "job_id": SITEMAP_REFRESH_JOB_ID,
                "stage": "discovering",
                "processed_sitemaps": refreshed_sitemaps,
                "urls_processed": discovered_urls,
                "requeued_urls": requeued_urls,
            }
            await self._runner.persist_checkpoint(
                execution_id=execution_id,
                checkpoint_data=last_checkpoint_data,
                urls_processed=discovered_urls,
            )

        summary = {
            "refreshed_sitemaps": refreshed_sitemaps,
            "discovered_urls": discovered_urls,
            "requeued_urls": requeued_urls,
        }
        return JobRunResult(
            summary=summary,
            urls_processed=discovered_urls,
            checkpoint_data=last_checkpoint_data,
        )

    async def _list_websites_with_credentials(
        self,
        *,
        requires_queued_urls: bool,
    ) -> list[_WebsiteCredentials]:
        async with session_scope() as session:
            statement = (
                select(Website.id, ServiceAccount.credentials_path)
                .join(ServiceAccount, ServiceAccount.website_id == Website.id)
                .where(Website.is_active.is_(True))
            )
            if requires_queued_urls:
                queued_url_count = (
                    select(func.count(URL.id))
                    .where(URL.website_id == Website.id, URL.current_priority > 0)
                    .scalar_subquery()
                )
                statement = statement.where(queued_url_count > 0)

            rows = (await session.execute(statement)).all()
            return [
                _WebsiteCredentials(website_id=row[0], credentials_path=row[1])
                for row in rows
            ]

    async def _pick_urls_for_verification(
        self,
        website_id: UUID,
    ) -> list[_VerificationCandidate]:
        latest_status = (
            select(
                IndexStatus.url_id.label("url_id"),
                func.max(IndexStatus.checked_at).label("last_checked_at"),
            )
            .group_by(IndexStatus.url_id)
            .subquery()
        )

        async with session_scope() as session:
            statement = (
                select(URL.id, URL.url, Website.site_url)
                .join(Website, Website.id == URL.website_id)
                .outerjoin(latest_status, latest_status.c.url_id == URL.id)
                .where(URL.website_id == website_id)
                .order_by(
                    nullsfirst(latest_status.c.last_checked_at),
                    URL.updated_at.desc(),
                )
                .limit(self._settings.SCHEDULER_INDEX_VERIFICATION_BATCH_SIZE)
            )
            rows = (await session.execute(statement)).all()
            return [
                _VerificationCandidate(url_id=row[0], url=row[1], site_url=row[2])
                for row in rows
            ]

    async def _inspect_single_url(
        self,
        *,
        website_id: UUID,
        candidate: _VerificationCandidate,
        client: _InspectionClient,
    ) -> IndexStatusResult:
        try:
            permit = await self._rate_limiter.acquire(website_id, api_type="inspection")
        except Exception as error:
            is_rate_limit_error = isinstance(
                error,
                (
                    RateLimitTokenUnavailableError,
                    RateLimitTimeoutError,
                    ConcurrentRequestLimitExceededError,
                ),
            )
            return IndexStatusResult(
                inspection_url=candidate.url,
                site_url=candidate.site_url,
                success=False,
                http_status=None,
                system_status=InspectionSystemStatus.UNKNOWN,
                verdict=None,
                coverage_state=None,
                last_crawl_time=None,
                indexing_state=None,
                robots_txt_state=None,
                raw_response=None,
                error_code="RATE_LIMITED"
                if is_rate_limit_error
                else "RATE_LIMITER_ERROR",
                error_message=str(error),
                retry_after_seconds=None,
            )

        try:
            inspect_url_callable = client.inspect_url
            async with session_scope() as session:
                try:
                    return await inspect_url_callable(
                        candidate.url,
                        candidate.site_url,
                        website_id=website_id,
                        session=session,
                    )
                except TypeError:
                    return await inspect_url_callable(candidate.url, candidate.site_url)
        except Exception as error:
            return IndexStatusResult(
                inspection_url=candidate.url,
                site_url=candidate.site_url,
                success=False,
                http_status=None,
                system_status=InspectionSystemStatus.UNKNOWN,
                verdict=None,
                coverage_state=None,
                last_crawl_time=None,
                indexing_state=None,
                robots_txt_state=None,
                raw_response=None,
                error_code="API_ERROR",
                error_message=str(error),
                retry_after_seconds=None,
            )
        finally:
            permit.release()

    @staticmethod
    def _index_status_row(
        *, url_id: UUID, result: IndexStatusResult
    ) -> dict[str, object]:
        return {
            "url_id": url_id,
            "coverage_state": result.coverage_state or "INSPECTION_FAILED",
            "verdict": SchedulerProcessingPipelineService._parse_verdict(
                result.verdict
            ),
            "last_crawl_time": result.last_crawl_time,
            "indexed_at": result.last_crawl_time,
            "checked_at": datetime.now(UTC),
            "robots_txt_state": result.robots_txt_state,
            "indexing_state": result.indexing_state,
            "page_fetch_state": None,
            "google_canonical": None,
            "user_canonical": None,
            "raw_response": result.raw_response
            or {
                "error_code": result.error_code,
                "error_message": result.error_message,
            },
        }

    @staticmethod
    def _parse_verdict(verdict: str | None) -> IndexVerdict:
        if verdict is None:
            return IndexVerdict.NEUTRAL

        normalized_verdict = verdict.strip().upper()
        if normalized_verdict in {
            IndexVerdict.PASS.value,
            IndexVerdict.FAIL.value,
            IndexVerdict.NEUTRAL.value,
            IndexVerdict.PARTIAL.value,
        }:
            return IndexVerdict(normalized_verdict)

        return IndexVerdict.NEUTRAL

    async def _list_active_sitemap_ids(self) -> list[UUID]:
        async with session_scope() as session:
            statement = (
                select(Sitemap.id)
                .join(Website, Website.id == Sitemap.website_id)
                .where(Sitemap.is_active.is_(True), Website.is_active.is_(True))
            )
            return list((await session.scalars(statement)).all())

    async def _list_sitemap_url_ids(self, sitemap_id: UUID) -> list[UUID]:
        async with session_scope() as session:
            statement = select(URL.id).where(URL.sitemap_id == sitemap_id)
            return list((await session.scalars(statement)).all())

    async def _log_activity(
        self,
        *,
        event_type: str,
        message: str,
        website_id: UUID | None,
        resource_type: str | None,
        resource_id: UUID | None,
        metadata: dict[str, Any] | None,
    ) -> None:
        async with session_scope() as session:
            await self._activity_service.log_activity(
                session=session,
                event_type=event_type,
                message=message,
                website_id=website_id,
                resource_type=resource_type,
                resource_id=resource_id,
                metadata=metadata,
            )


def set_scheduler_processing_pipeline_service(
    service: SchedulerProcessingPipelineService,
) -> None:
    global _pipeline_service
    _pipeline_service = service


def _require_pipeline_service() -> SchedulerProcessingPipelineService:
    if _pipeline_service is None:
        raise RuntimeError("Scheduler processing pipeline service is not initialized")

    return _pipeline_service


async def run_scheduled_url_submission_job() -> None:
    await _require_pipeline_service().run_url_submission_job()


async def run_scheduled_index_verification_job() -> None:
    await _require_pipeline_service().run_index_verification_job()


async def run_scheduled_sitemap_refresh_job() -> None:
    await _require_pipeline_service().run_sitemap_refresh_job()


__all__ = [
    "INDEX_VERIFICATION_JOB_ID",
    "JobExecutionMetrics",
    "SITEMAP_REFRESH_JOB_ID",
    "SchedulerProcessingPipelineService",
    "URL_SUBMISSION_JOB_ID",
    "run_scheduled_index_verification_job",
    "run_scheduled_sitemap_refresh_job",
    "run_scheduled_url_submission_job",
    "set_scheduler_processing_pipeline_service",
]
