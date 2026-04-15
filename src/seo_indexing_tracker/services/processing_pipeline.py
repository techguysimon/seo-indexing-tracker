"""Scheduled processing pipeline jobs for submission, verification, and refresh."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import and_, case, func, insert, or_, select
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.config import Settings
from seo_indexing_tracker.database import session_scope
from seo_indexing_tracker.models import (
    IndexStatus,
    ServiceAccount,
    Sitemap,
    URL,
    Website,
)
from seo_indexing_tracker.services.activity_service import ActivityService
from seo_indexing_tracker.services.batch_processor import BatchProcessorService
from seo_indexing_tracker.services.cooldown_service import CooldownService
from seo_indexing_tracker.services.google_api_factory import (
    WebsiteGoogleAPIClients,
    WebsiteServiceAccountConfig,
)
from seo_indexing_tracker.services.google_url_inspection_client import (
    IndexStatusResult,
    InspectionSystemStatus,
)
from seo_indexing_tracker.services.job_runner import (
    JobExecutionMetrics,
    JobRunResult,
    JobRunnerService,
)
from seo_indexing_tracker.services.priority_queue import PriorityQueueService
from seo_indexing_tracker.services.quota_service import (
    DailyQuotaExceededError,
    QuotaService,
)
from seo_indexing_tracker.services.rate_limiter import (
    ConcurrentRequestLimitExceededError,
    RateLimiterService,
    RateLimitTimeoutError,
    RateLimitTokenUnavailableError,
)
from seo_indexing_tracker.services.scheduler import SchedulerService
from seo_indexing_tracker.services.url_discovery import URLDiscoveryService
from seo_indexing_tracker.utils.index_status import (
    derive_url_index_status_from_coverage_state,
)
from seo_indexing_tracker.utils.job_helpers import (
    build_index_status_row,
    is_transient_quota_error_code,
)

_job_logger = logging.getLogger("seo_indexing_tracker.scheduler.jobs")

_pipeline_service: SchedulerProcessingPipelineService | None = None

URL_SUBMISSION_JOB_ID = "url-submission-job"
INDEX_VERIFICATION_JOB_ID = "index-verification-job"
SITEMAP_REFRESH_JOB_ID = "sitemap-refresh-job"
DEFAULT_RATE_LIMIT_ACQUIRE_TIMEOUT_SECONDS = 10.0


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
        self._cooldown_service = CooldownService(settings=settings)
        self._runner = JobRunnerService()

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
        """Submit URLs that have been verified as NOT_INDEXED.

        This job does NOT inspect URLs — that's the verification job's job.
        It only queries URLs with latest_index_status = NOT_INDEXED and
        submits them to the Indexing API. This decouples inspection quota
        from submission, preventing rate-limit cascades.
        """
        website_credentials = await self._list_websites_with_credentials(
            requires_queued_urls=False
        )
        _job_logger.debug(
            "submit_urls_websites_found",
            extra={
                "website_count": len(website_credentials),
                "website_ids": [str(w.id) for w in website_credentials],
            },
        )
        if not website_credentials:
            _job_logger.debug("submit_urls_no_websites_with_credentials")
        processed_websites = 0
        submitted_urls = 0
        failed_urls = 0
        last_checkpoint_data: dict[str, Any] | None = {
            "job_id": URL_SUBMISSION_JOB_ID,
            "stage": "initialized",
            "processed_websites": 0,
            "urls_processed": 0,
        }

        for website in website_credentials:
            cooldown_window = self._cooldown_service.get_cooldown_window(website)
            if cooldown_window is not None:
                # Build metadata based on cooldown type
                if cooldown_window.is_internal_rate_limit:
                    cooldown_metadata = {
                        "website_domain": website.domain,
                        "cooldown_type": "internal_rate_limit",
                        "internal_rate_limit_at": cooldown_window.last_429_at.isoformat(),
                        "cooldown_seconds": cooldown_window.cooldown_seconds,
                        "next_allowed_at": cooldown_window.next_allowed_at.isoformat(),
                    }
                    log_message = "Skipped URL submission because internal rate-limit cooldown is active"
                else:
                    cooldown_metadata = {
                        "website_domain": website.domain,
                        "cooldown_type": "google_429",
                        "quota_last_429_at": cooldown_window.last_429_at.isoformat(),
                        "cooldown_seconds": cooldown_window.cooldown_seconds,
                        "next_allowed_at": cooldown_window.next_allowed_at.isoformat(),
                    }
                    log_message = (
                        "Skipped URL submission because Google 429 cooldown is active"
                    )

                await self._log_activity(
                    event_type="url_submission_skipped_rate_limited",
                    website_id=website.id,
                    resource_type="website",
                    resource_id=website.id,
                    message=log_message,
                    metadata=cooldown_metadata,
                )
                _job_logger.info(
                    "submit_urls_skipped_rate_limit_cooldown",
                    extra={
                        "website_id": str(website.id),
                        "website_domain": website.domain,
                        **cooldown_metadata,
                    },
                )
                continue

            _job_logger.debug(
                "submit_urls_processing_website",
                extra={
                    "website_id": str(website.id),
                    "batch_size": self._settings.SCHEDULER_URL_SUBMISSION_BATCH_SIZE,
                },
            )
            assert website.service_account is not None
            self._client_factory.register_website(
                website_id=website.id,
                credentials_path=website.service_account.credentials_path,
            )

            result = await self._batch_processor.submit_not_indexed_batch(
                website.id,
                requested_urls=self._settings.SCHEDULER_URL_SUBMISSION_BATCH_SIZE,
            )
            _job_logger.debug(
                "submit_urls_batch_complete",
                extra={
                    "website_id": str(website.id),
                    "dequeued_urls": result.dequeued_urls,
                    "submission_success_count": result.submission_success_count,
                    "submission_failure_count": result.submission_failure_count,
                },
            )
            processed_websites += 1
            submitted_urls += result.submission_success_count
            failed_urls += result.submission_failure_count
            await self._log_activity(
                event_type="url_submitted",
                website_id=website.id,
                resource_type="website",
                resource_id=website.id,
                message=(
                    f"Submitted {result.submission_success_count} URLs "
                    f"for website {website.id}"
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
                "website_id": str(website.id),
                "processed_websites": processed_websites,
                "urls_processed": submitted_urls,
            }
            await self._runner.persist_checkpoint(
                execution_id=execution_id,
                checkpoint_data=last_checkpoint_data,
                urls_processed=submitted_urls,
            )

        summary = {
            "processed_websites": processed_websites,
            "submitted_urls": submitted_urls,
            "failed_urls": failed_urls,
        }
        return JobRunResult(
            summary=summary,
            urls_processed=submitted_urls,
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
                "website_ids": [str(w.id) for w in website_credentials],
            },
        )
        if not website_credentials:
            _job_logger.debug("verify_index_no_websites_with_credentials")
        inspected_urls = 0
        failed_urls = 0

        for website in website_credentials:
            cooldown_window = self._cooldown_service.get_cooldown_window(website)
            if cooldown_window is not None:
                # Build log extra based on cooldown type
                if cooldown_window.is_internal_rate_limit:
                    log_extra = {
                        "website_id": str(website.id),
                        "website_domain": website.domain,
                        "cooldown_type": "internal_rate_limit",
                        "internal_rate_limit_at": cooldown_window.last_429_at.isoformat(),
                        "cooldown_seconds": cooldown_window.cooldown_seconds,
                        "next_allowed_at": cooldown_window.next_allowed_at.isoformat(),
                        "job": "verification",
                    }
                else:
                    log_extra = {
                        "website_id": str(website.id),
                        "website_domain": website.domain,
                        "cooldown_type": "google_429",
                        "quota_last_429_at": cooldown_window.last_429_at.isoformat(),
                        "cooldown_seconds": cooldown_window.cooldown_seconds,
                        "next_allowed_at": cooldown_window.next_allowed_at.isoformat(),
                        "job": "verification",
                    }
                _job_logger.info(
                    "verify_index_skipped_rate_limit_cooldown",
                    extra=log_extra,
                )
                continue

            _job_logger.debug(
                "verify_index_processing_website",
                extra={"website_id": str(website.id)},
            )
            assert website.service_account is not None
            self._client_factory.register_website(
                website_id=website.id,
                credentials_path=website.service_account.credentials_path,
            )
            candidate_urls = await self._pick_urls_for_verification(website.id)
            _job_logger.debug(
                "verify_index_candidates_found",
                extra={
                    "website_id": str(website.id),
                    "candidate_count": len(candidate_urls),
                },
            )
            if not candidate_urls:
                continue

            inspection_client = self._client_factory.get_client(
                website.id
            ).search_console
            inspection_rows: list[dict[str, object]] = []
            results_by_url_id: dict[UUID, IndexStatusResult] = {}
            saw_google_429 = False
            for candidate in candidate_urls:
                result = await self._inspect_single_url(
                    website_id=website.id,
                    candidate=candidate,
                    client=inspection_client,
                )
                if result.http_status == 429:
                    saw_google_429 = True
                inspection_rows.append(
                    build_index_status_row(url_id=candidate.url_id, result=result)
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
                website_row = await session.get(Website, website.id)
                if website_row is not None:
                    # Only set quota_last_429_at for actual Google HTTP 429 responses
                    # This triggers the long cooldown for genuine quota exhaustion
                    if saw_google_429:
                        website_row.quota_last_429_at = checked_at
                    # NOTE: Do NOT set internal_rate_limit_at here.
                    # The verification job (Inspection API) and submission job
                    # (Indexing API) have separate Google quotas. A rate limit
                    # on inspection should not block submissions.

                for url_id, result in results_by_url_id.items():
                    url = url_by_id.get(url_id)
                    if url is None:
                        continue
                    if is_transient_quota_error_code(result.error_code):
                        # Preserve denormalized URL status during transient quota/rate-limit outages.
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

            # Only enqueue URLs that are genuinely new or changed in this refresh.
            # Enqueuing ALL sitemap URLs every hour re-queues already-indexed URLs,
            # which burns inspection quota in the verify-first submission workflow
            # without ever submitting anything.
            url_ids_to_enqueue = list(discovery_result.new_url_ids) + list(
                discovery_result.modified_url_ids
            )
            if not url_ids_to_enqueue:
                continue

            discovered_urls += len(url_ids_to_enqueue)
            requeued_urls += await self._queue_service.enqueue_many(url_ids_to_enqueue)
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
    ) -> list[Website]:
        async with session_scope() as session:
            statement = (
                select(Website)
                .options(joinedload(Website.service_account))
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

            result = await session.execute(statement)
            return list(result.unique().scalars().all())

    async def _pick_urls_for_verification(
        self,
        website_id: UUID,
    ) -> list[_VerificationCandidate]:
        """Pick URLs for verification, prioritizing UNCHECKED URLs.

        Order: UNCHECKED (never verified) > NOT_INDEXED > ERROR > INDEXED (re-verify).
        This ensures the backlog of unchecked URLs is cleared first, which is
        critical for the verify-then-submit pipeline.
        """
        indexed_reverification_cutoff = datetime.now(UTC) - timedelta(
            seconds=self._cooldown_service.get_indexed_reverification_min_age()
        )
        async with session_scope() as session:
            statement = (
                select(URL.id, URL.url, Website.site_url)
                .join(Website, Website.id == URL.website_id)
                .where(
                    URL.website_id == website_id,
                    or_(
                        URL.latest_index_status.in_(
                            ["UNCHECKED", "NOT_INDEXED", "ERROR"]
                        ),
                        and_(
                            URL.latest_index_status == "INDEXED",
                            or_(
                                URL.last_checked_at.is_(None),
                                URL.last_checked_at <= indexed_reverification_cutoff,
                            ),
                        ),
                    ),
                )
                .order_by(
                    case(
                        (URL.latest_index_status == "UNCHECKED", 0),
                        (URL.latest_index_status == "NOT_INDEXED", 1),
                        (URL.latest_index_status == "ERROR", 2),
                        (URL.latest_index_status == "INDEXED", 3),
                        else_=4,
                    ),
                    case(
                        (URL.last_checked_at.is_(None), 0),
                        else_=1,
                    ),
                    URL.last_checked_at.asc(),
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
            permit = await self._acquire_inspection_rate_limit_permit(
                website_id=website_id
            )
        except DailyQuotaExceededError as error:
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
                error_code="QUOTA_EXCEEDED",
                error_message=str(error),
                retry_after_seconds=None,
            )
        except (
            RateLimitTimeoutError,
            RateLimitTokenUnavailableError,
            ConcurrentRequestLimitExceededError,
            TimeoutError,
        ) as error:
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
                error_code="RATE_LIMITED",
                error_message=str(error),
                retry_after_seconds=None,
            )
        except Exception:
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
                error_code="RATE_LIMITER_ERROR",
                error_message="Failed to acquire inspection rate-limit permit",
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

    def _inspection_acquire_timeout_seconds(self) -> float:
        configured_timeout = getattr(
            self._settings,
            "RATE_LIMIT_ACQUIRE_TIMEOUT_SECONDS",
            None,
        )
        if isinstance(configured_timeout, int | float) and configured_timeout > 0:
            return float(configured_timeout)

        return DEFAULT_RATE_LIMIT_ACQUIRE_TIMEOUT_SECONDS

    async def _acquire_inspection_rate_limit_permit(self, *, website_id: UUID) -> Any:
        timeout_seconds = self._inspection_acquire_timeout_seconds()
        try:
            return await self._rate_limiter.acquire(
                website_id,
                api_type="inspection",
                timeout_seconds=timeout_seconds,
            )
        except TypeError as error:
            if "timeout_seconds" not in str(error):
                raise

            return await self._rate_limiter.acquire(website_id, api_type="inspection")

    async def _list_active_sitemap_ids(self) -> list[UUID]:
        async with session_scope() as session:
            statement = (
                select(Sitemap.id)
                .join(Website, Website.id == Sitemap.website_id)
                .where(Sitemap.is_active.is_(True), Website.is_active.is_(True))
            )
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
    "JobRunResult",
    "JobRunnerService",
    "SITEMAP_REFRESH_JOB_ID",
    "SchedulerProcessingPipelineService",
    "URL_SUBMISSION_JOB_ID",
    "run_scheduled_index_verification_job",
    "run_scheduled_sitemap_refresh_job",
    "run_scheduled_url_submission_job",
    "set_scheduler_processing_pipeline_service",
]
