"""Batch processing service for URL dequeue, submit, and inspection workflows."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
import logging
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import and_, func, insert, select, update as update_stmt
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.models import (
    IndexStatus,
    SubmissionAction,
    SubmissionLog,
    SubmissionStatus,
    URL,
    Website,
)
from seo_indexing_tracker.utils.index_status import (
    derive_url_index_status_from_coverage_state,
)
from seo_indexing_tracker.utils.shared_helpers import (
    extract_index_status_result,
    optional_text,
    parse_verdict,
)
from seo_indexing_tracker.services.google_indexing_client import (
    BatchSubmitResult,
    IndexingURLResult,
    MAX_BATCH_SUBMIT_SIZE,
)
from seo_indexing_tracker.services.google_url_inspection_client import (
    IndexStatusResult,
    InspectionSystemStatus,
)
from seo_indexing_tracker.services.priority_queue import PriorityQueueService
from seo_indexing_tracker.services.rate_limiter import (
    RateLimitPermit,
    RateLimiterService,
)

SessionScopeFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
ProgressCallback = Callable[["BatchProgressUpdate"], Awaitable[None] | None]

_batch_logger = logging.getLogger("seo_indexing_tracker.batch_processor")

DEFAULT_DEQUEUE_BATCH_SIZE = 100
DEFAULT_SUBMIT_BATCH_SIZE = 100
DEFAULT_INSPECTION_BATCH_SIZE = 25
DEFAULT_RATE_LIMIT_ACQUIRE_TIMEOUT_SECONDS = 10.0


class BatchProcessorStatus(str, Enum):
    """High-level outcome for a batch processing run."""

    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


@dataclass(slots=True, frozen=True)
class BatchProgressUpdate:
    """Incremental status information for a batch processing run."""

    website_id: UUID
    stage: str
    message: str
    total_urls: int
    processed_urls: int
    successful_urls: int
    failed_urls: int
    checkpoint_data: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class URLBatchOutcome:
    """Per-URL outcome for submission and inspection steps."""

    url_id: UUID
    url: str
    submission_success: bool
    submission_skipped: bool
    submission_error_code: str | None
    submission_error_message: str | None
    inspection_attempted: bool
    inspection_success: bool
    inspection_error_code: str | None
    inspection_error_message: str | None


@dataclass(slots=True, frozen=True)
class BatchProcessingResult:
    """Final aggregate result for one batch processor execution."""

    website_id: UUID
    requested_urls: int
    dequeued_urls: int
    submitted_urls: int
    submission_success_count: int
    submission_failure_count: int
    inspected_urls: int
    inspection_success_count: int
    inspection_failure_count: int
    requeued_urls: int
    status: BatchProcessorStatus
    outcomes: list[URLBatchOutcome]


@dataclass(slots=True, frozen=True)
class _SubmissionLogRecord:
    url_id: UUID
    api_response: dict[str, Any]
    status: SubmissionStatus
    error_message: str | None


@dataclass(slots=True, frozen=True)
class _InspectionRecord:
    url_id: UUID
    result: IndexStatusResult


class _IndexingBatchClient(Protocol):
    async def batch_submit(
        self,
        urls: list[str] | tuple[str, ...],
        action: str = "URL_UPDATED",
        *,
        website_id: UUID | None = None,
        session: AsyncSession | None = None,
    ) -> BatchSubmitResult: ...


class _URLInspectionClient(Protocol):
    async def inspect_url(
        self,
        url: str,
        site_url: str,
        *,
        website_id: UUID | None = None,
        session: AsyncSession | None = None,
    ) -> IndexStatusResult: ...


class _GoogleClientBundle(Protocol):
    indexing: _IndexingBatchClient
    search_console: _URLInspectionClient


class _GoogleClientFactory(Protocol):
    def get_client(self, website_id: UUID | str) -> _GoogleClientBundle: ...


class BatchProcessorService:
    """Run website-scoped URL batches through submission and inspection."""

    def __init__(
        self,
        *,
        priority_queue: PriorityQueueService,
        client_factory: _GoogleClientFactory,
        rate_limiter: RateLimiterService,
        session_factory: SessionScopeFactory | None = None,
        dequeue_batch_size: int = DEFAULT_DEQUEUE_BATCH_SIZE,
        submit_batch_size: int = DEFAULT_SUBMIT_BATCH_SIZE,
        inspection_batch_size: int = DEFAULT_INSPECTION_BATCH_SIZE,
        rate_limit_acquire_timeout_seconds: float | None = None,
    ) -> None:
        if dequeue_batch_size <= 0:
            raise ValueError("dequeue_batch_size must be greater than zero")
        if submit_batch_size <= 0:
            raise ValueError("submit_batch_size must be greater than zero")
        if submit_batch_size > MAX_BATCH_SUBMIT_SIZE:
            raise ValueError(
                "submit_batch_size cannot exceed Google API max batch size "
                f"({MAX_BATCH_SUBMIT_SIZE})"
            )
        if inspection_batch_size <= 0:
            raise ValueError("inspection_batch_size must be greater than zero")
        if (
            rate_limit_acquire_timeout_seconds is not None
            and rate_limit_acquire_timeout_seconds <= 0
        ):
            raise ValueError(
                "rate_limit_acquire_timeout_seconds must be greater than zero"
            )

        if session_factory is None:
            from seo_indexing_tracker.database import session_scope

            session_factory = session_scope

        self._priority_queue = priority_queue
        self._client_factory = client_factory
        self._rate_limiter = rate_limiter
        self._session_factory = session_factory
        self._dequeue_batch_size = dequeue_batch_size
        self._submit_batch_size = submit_batch_size
        self._inspection_batch_size = inspection_batch_size
        self._rate_limit_acquire_timeout_seconds = (
            rate_limit_acquire_timeout_seconds
            if rate_limit_acquire_timeout_seconds is not None
            else self._default_acquire_timeout_seconds()
        )

    async def process_batch(
        self,
        website_id: UUID,
        *,
        requested_urls: int | None = None,
        action: SubmissionAction = SubmissionAction.URL_UPDATED,
        progress_callback: ProgressCallback | None = None,
    ) -> BatchProcessingResult:
        """Process one website queue batch and return detailed outcomes."""

        if requested_urls is not None and requested_urls <= 0:
            raise ValueError("requested_urls must be greater than zero")

        dequeue_limit = requested_urls or self._dequeue_batch_size
        website = await self._get_website_or_raise(website_id)
        queued_urls = await self._priority_queue.dequeue(
            website_id, limit=dequeue_limit
        )
        if not queued_urls:
            return self._empty_result(
                website_id=website_id, requested_urls=dequeue_limit
            )

        outcomes = self._initial_outcomes(queued_urls)
        ordered_url_ids = [queued_url.id for queued_url in queued_urls]

        await self._emit_progress(
            callback=progress_callback,
            website_id=website_id,
            stage="dequeue",
            message="Dequeued URLs for batch processing",
            total_urls=len(queued_urls),
            processed_urls=0,
            successful_urls=0,
            failed_urls=0,
        )

        finalization_completed = False
        try:
            try:
                clients = self._client_factory.get_client(website_id)
            except Exception as error:
                failure_outcomes = {
                    url_id: self._with_submission_failure(
                        outcomes[url_id],
                        error_code="CLIENT_INIT_FAILED",
                        error_message=str(error),
                    )
                    for url_id in ordered_url_ids
                }
                requeued_count = await self._priority_queue.enqueue_many(
                    ordered_url_ids
                )
                return BatchProcessingResult(
                    website_id=website_id,
                    requested_urls=dequeue_limit,
                    dequeued_urls=len(queued_urls),
                    submitted_urls=0,
                    submission_success_count=0,
                    submission_failure_count=len(queued_urls),
                    inspected_urls=0,
                    inspection_success_count=0,
                    inspection_failure_count=0,
                    requeued_urls=requeued_count,
                    status=BatchProcessorStatus.FAILED,
                    outcomes=[failure_outcomes[url_id] for url_id in ordered_url_ids],
                )

            # VERIFY-FIRST WORKFLOW: Inspect ALL dequeued URLs before submission
            # This saves quota by avoiding submission of already-indexed URLs
            inspection_records: list[_InspectionRecord] = []
            inspection_results_by_url: dict[UUID, IndexStatusResult] = {}
            inspected_count = 0

            for inspect_batch in self._chunk_urls(
                queued_urls, self._inspection_batch_size
            ):
                batch_inspection_results = await self._inspect_url_batch(
                    website_id=website_id,
                    site_url=website.site_url,
                    urls=inspect_batch,
                    client=clients.search_console,
                )
                inspection_results_by_url.update(batch_inspection_results)

                for inspected_url in inspect_batch:
                    inspection_result = batch_inspection_results[inspected_url.id]
                    outcomes[inspected_url.id] = self._with_inspection_result(
                        outcomes[inspected_url.id],
                        inspection_result,
                    )
                    inspection_records.append(
                        _InspectionRecord(
                            url_id=inspected_url.id, result=inspection_result
                        )
                    )

                inspected_count += len(inspect_batch)
                await self._emit_progress(
                    callback=progress_callback,
                    website_id=website_id,
                    stage="inspect",
                    message="Pre-submission URL inspection batch completed",
                    total_urls=len(queued_urls),
                    processed_urls=inspected_count,
                    successful_urls=self._inspection_success_count(
                        list(outcomes.values())
                    ),
                    failed_urls=inspected_count
                    - self._inspection_success_count(list(outcomes.values())),
                )

            if any(
                self._is_transient_quota_error_code(record.result.error_code)
                for record in inspection_records
            ):
                await self._mark_website_internal_rate_limited(website_id)

            # Record all inspection results (updates URL.latest_index_status)
            await self._record_index_statuses(records=inspection_records)

            # Filter URLs for submission based on inspection results
            # Skip URLs that are already indexed, unless user forced submission via manual_priority_override
            submitted_results: dict[UUID, IndexingURLResult] = {}
            submission_logs: list[_SubmissionLogRecord] = []
            eligible_for_submission: list[URL] = []
            next_checkpoint_at = 100

            for queued_url in queued_urls:
                url_inspection_result: IndexStatusResult | None = (
                    inspection_results_by_url.get(queued_url.id)
                )

                # Check if URL is already indexed based on fresh inspection result
                is_indexed = self._inspection_shows_indexed(url_inspection_result)

                # User override: always submit if manual_priority_override is set
                if queued_url.manual_priority_override is not None:
                    eligible_for_submission.append(queued_url)
                    continue

                if is_indexed:
                    outcomes[queued_url.id] = self._with_submission_skipped(
                        outcomes[queued_url.id],
                        error_code="SKIPPED_ALREADY_INDEXED",
                        error_message="Skipped submission because inspection shows URL is already indexed",
                    )
                    submission_logs.append(
                        _SubmissionLogRecord(
                            url_id=queued_url.id,
                            api_response={
                                "url": queued_url.url,
                                "action": action.value,
                                "success": False,
                                "error_code": "SKIPPED_ALREADY_INDEXED",
                                "error_message": (
                                    "Skipped submission because inspection shows URL is already indexed"
                                ),
                            },
                            status=SubmissionStatus.SKIPPED,
                            error_message="Skipped submission because inspection shows URL is already indexed",
                        )
                    )
                    continue

                # If inspection failed, fall back to submitting (conservative: better to submit than miss)
                if (
                    url_inspection_result is not None
                    and not url_inspection_result.success
                ):
                    _batch_logger.warning(
                        "inspection_failed_falling_back_to_submission",
                        extra={
                            "url_id": str(queued_url.id),
                            "url": queued_url.url,
                            "error_code": url_inspection_result.error_code,
                        },
                    )

                eligible_for_submission.append(queued_url)

            # Submit only the filtered URLs that need it
            for url_batch in self._chunk_urls(
                eligible_for_submission, self._submit_batch_size
            ):
                batch_results, batch_logs = await self._submit_url_batch(
                    website_id=website_id,
                    urls=url_batch,
                    action=action,
                    client=clients.indexing,
                )
                submitted_results.update(batch_results)
                submission_logs.extend(batch_logs)

                for url_id, submission_result in batch_results.items():
                    outcomes[url_id] = self._with_submission_result(
                        outcomes[url_id], submission_result
                    )

                await self._emit_progress(
                    callback=progress_callback,
                    website_id=website_id,
                    stage="submit",
                    message="Submitted URL batch to Google Indexing API",
                    total_urls=len(queued_urls),
                    processed_urls=(
                        len(submitted_results)
                        + self._submission_skipped_count(list(outcomes.values()))
                    ),
                    successful_urls=self._submission_success_count(
                        list(outcomes.values())
                    ),
                    failed_urls=(
                        len(submitted_results)
                        - self._submission_success_count(list(outcomes.values()))
                    ),
                )
                next_checkpoint_at = await self._emit_checkpoint_if_due(
                    callback=progress_callback,
                    website_id=website_id,
                    total_urls=len(queued_urls),
                    processed_urls=(
                        len(submitted_results)
                        + self._submission_skipped_count(list(outcomes.values()))
                    ),
                    successful_urls=self._submission_success_count(
                        list(outcomes.values())
                    ),
                    failed_urls=(
                        len(submitted_results)
                        - self._submission_success_count(list(outcomes.values()))
                    ),
                    next_checkpoint_at=next_checkpoint_at,
                )

            await self._record_submission_logs(action=action, logs=submission_logs)

            failed_url_ids = [
                outcome.url_id
                for outcome in outcomes.values()
                if self._should_requeue_outcome(outcome)
            ]
            requeued_count = await self._priority_queue.enqueue_many(failed_url_ids)
            finalization_completed = True

            final_outcomes = [outcomes[url_id] for url_id in ordered_url_ids]
            submission_success_count = self._submission_success_count(final_outcomes)
            inspected_outcomes = [
                outcome for outcome in final_outcomes if outcome.inspection_attempted
            ]
            inspection_success_count = self._inspection_success_count(final_outcomes)

            status = self._derive_final_status(final_outcomes)
            await self._emit_progress(
                callback=progress_callback,
                website_id=website_id,
                stage="completed",
                message="Batch processing completed",
                total_urls=len(final_outcomes),
                processed_urls=len(final_outcomes),
                successful_urls=inspection_success_count,
                failed_urls=len(final_outcomes) - inspection_success_count,
            )

            return BatchProcessingResult(
                website_id=website_id,
                requested_urls=dequeue_limit,
                dequeued_urls=len(final_outcomes),
                submitted_urls=len(submitted_results),
                submission_success_count=submission_success_count,
                submission_failure_count=len(submitted_results)
                - submission_success_count,
                inspected_urls=len(inspected_outcomes),
                inspection_success_count=inspection_success_count,
                inspection_failure_count=len(inspected_outcomes)
                - inspection_success_count,
                requeued_urls=requeued_count,
                status=status,
                outcomes=final_outcomes,
            )
        except asyncio.CancelledError:
            await self._requeue_unfinished_urls(
                ordered_url_ids=ordered_url_ids,
                outcomes=outcomes,
                finalization_completed=finalization_completed,
            )
            raise
        except Exception:
            await self._requeue_unfinished_urls(
                ordered_url_ids=ordered_url_ids,
                outcomes=outcomes,
                finalization_completed=finalization_completed,
            )
            raise

    async def _submit_url_batch(
        self,
        *,
        website_id: UUID,
        urls: Sequence[URL],
        action: SubmissionAction,
        client: _IndexingBatchClient,
    ) -> tuple[dict[UUID, IndexingURLResult], list[_SubmissionLogRecord]]:
        if not urls:
            return {}, []

        results: dict[UUID, IndexingURLResult] = {}
        logs: list[_SubmissionLogRecord] = []
        permits: list[RateLimitPermit] = []
        submittable_urls: list[URL] = []
        observed_rate_limit = False

        for url in urls:
            try:
                permit = await self._acquire_rate_limit_permit(
                    website_id=website_id,
                    api_type="indexing",
                )
            except Exception as error:
                observed_rate_limit = True
                result = self._submission_failure_result(
                    url=url.url,
                    action=action,
                    error_code="RATE_LIMITED",
                    error_message=str(error),
                )
                results[url.id] = result
                logs.append(
                    _SubmissionLogRecord(
                        url_id=url.id,
                        api_response=asdict(result),
                        status=SubmissionStatus.RATE_LIMITED,
                        error_message=result.error_message,
                    )
                )
                continue
            permits.append(permit)
            submittable_urls.append(url)

        if not submittable_urls:
            if observed_rate_limit:
                await self._mark_website_internal_rate_limited(website_id)
            return results, logs

        try:
            async with self._session_factory() as session:
                try:
                    batch_result = await client.batch_submit(
                        [url.url for url in submittable_urls],
                        action.value,
                        website_id=website_id,
                        session=session,
                    )
                except TypeError:
                    batch_result = await client.batch_submit(
                        [url.url for url in submittable_urls],
                        action.value,
                    )
        except Exception as error:
            for url in submittable_urls:
                result = self._submission_failure_result(
                    url=url.url,
                    action=action,
                    error_code="API_ERROR",
                    error_message=str(error),
                )
                results[url.id] = result
                logs.append(
                    _SubmissionLogRecord(
                        url_id=url.id,
                        api_response=asdict(result),
                        status=SubmissionStatus.FAILED,
                        error_message=result.error_message,
                    )
                )
            return results, logs
        finally:
            for permit in permits:
                permit.release()

        result_by_url = {result.url: result for result in batch_result.results}
        for url in submittable_urls:
            found_result = result_by_url.get(url.url)
            submission_result: IndexingURLResult
            if found_result is None:
                submission_result = self._submission_failure_result(
                    url=url.url,
                    action=action,
                    error_code="API_ERROR",
                    error_message="Missing URL result in batch submission response",
                )
            else:
                submission_result = found_result
                if submission_result.error_code in {"QUOTA_EXCEEDED", "RATE_LIMITED"}:
                    observed_rate_limit = True

            results[url.id] = submission_result
            logs.append(
                _SubmissionLogRecord(
                    url_id=url.id,
                    api_response=asdict(submission_result),
                    status=self._submission_status_from_result(submission_result),
                    error_message=submission_result.error_message,
                )
            )

        if observed_rate_limit:
            await self._mark_website_internal_rate_limited(website_id)

        return results, logs

    async def _mark_website_internal_rate_limited(self, website_id: UUID) -> None:
        """Mark website as internally rate-limited (token bucket exhausted).

        This is distinct from Google HTTP 429 responses - internal rate limiting
        is our own backpressure mechanism, not Google rejecting requests.
        """
        async with self._session_factory() as session:
            website = await session.get(Website, website_id)
            if website is None:
                return

            website.internal_rate_limit_at = datetime.now(UTC)
            await session.flush()

    async def _inspect_url_batch(
        self,
        *,
        website_id: UUID,
        site_url: str,
        urls: Sequence[URL],
        client: _URLInspectionClient,
    ) -> dict[UUID, IndexStatusResult]:
        if not urls:
            return {}

        records = await asyncio.gather(
            *[
                self._inspect_single_url(
                    website_id=website_id,
                    site_url=site_url,
                    url=url,
                    client=client,
                )
                for url in urls
            ]
        )
        return {url_id: inspection_result for url_id, inspection_result in records}

    async def _inspect_single_url(
        self,
        *,
        website_id: UUID,
        site_url: str,
        url: URL,
        client: _URLInspectionClient,
    ) -> tuple[UUID, IndexStatusResult]:
        try:
            permit = await self._acquire_rate_limit_permit(
                website_id=website_id,
                api_type="inspection",
            )
        except Exception as error:
            return (
                url.id,
                self._inspection_failure_result(
                    inspection_url=url.url,
                    site_url=site_url,
                    error_code="RATE_LIMITED",
                    error_message=str(error),
                ),
            )

        try:
            async with self._session_factory() as session:
                try:
                    return url.id, await client.inspect_url(
                        url.url,
                        site_url,
                        website_id=website_id,
                        session=session,
                    )
                except TypeError:
                    return url.id, await client.inspect_url(url.url, site_url)
        except Exception as error:
            return (
                url.id,
                self._inspection_failure_result(
                    inspection_url=url.url,
                    site_url=site_url,
                    error_code="API_ERROR",
                    error_message=str(error),
                ),
            )
        finally:
            permit.release()

    async def _record_submission_logs(
        self,
        *,
        action: SubmissionAction,
        logs: Sequence[_SubmissionLogRecord],
    ) -> None:
        if not logs:
            return

        rows = [
            {
                "url_id": log.url_id,
                "action": action,
                "api_response": log.api_response,
                "status": log.status,
                "error_message": log.error_message,
            }
            for log in logs
        ]
        async with self._session_factory() as session:
            await session.execute(insert(SubmissionLog), rows)

            # Update last_submitted_at for successful submissions
            successful_url_ids = [
                log.url_id for log in logs if log.status == SubmissionStatus.SUCCESS
            ]
            if successful_url_ids:
                await session.execute(
                    update_stmt(URL)
                    .where(URL.id.in_(successful_url_ids))
                    .values(last_submitted_at=datetime.now(UTC))
                )

    async def _record_index_statuses(
        self, *, records: Sequence[_InspectionRecord]
    ) -> None:
        if not records:
            return

        checked_at = datetime.now(UTC)
        rows = [
            self._index_status_row(record=record, checked_at=checked_at)
            for record in records
        ]
        async with self._session_factory() as session:
            await session.execute(insert(IndexStatus), rows)

            url_ids = [record.url_id for record in records]
            urls = await session.scalars(select(URL).where(URL.id.in_(url_ids)))
            url_by_id = {url.id: url for url in urls}

            for record in records:
                url = url_by_id.get(record.url_id)
                if url is None:
                    continue
                if self._is_transient_quota_error_code(record.result.error_code):
                    # Preserve last known denormalized status when inspection fails transiently.
                    continue
                coverage_state = record.result.coverage_state or "INSPECTION_FAILED"
                derived_status = derive_url_index_status_from_coverage_state(
                    coverage_state
                )
                url.latest_index_status = derived_status
                url.last_checked_at = checked_at

    @staticmethod
    def _is_transient_quota_error_code(error_code: str | None) -> bool:
        return error_code in {"RATE_LIMITED", "QUOTA_EXCEEDED"}

    def _index_status_row(
        self,
        *,
        record: _InspectionRecord,
        checked_at: datetime,
    ) -> dict[str, Any]:
        result = record.result
        raw_response = result.raw_response or {}
        index_status_result = extract_index_status_result(raw_response)

        return {
            "url_id": record.url_id,
            "coverage_state": result.coverage_state or "INSPECTION_FAILED",
            "verdict": parse_verdict(result.verdict),
            "last_crawl_time": result.last_crawl_time,
            "indexed_at": result.last_crawl_time,
            "checked_at": checked_at,
            "robots_txt_state": result.robots_txt_state,
            "indexing_state": result.indexing_state,
            "page_fetch_state": optional_text(
                index_status_result.get("pageFetchState")
            ),
            "google_canonical": optional_text(
                index_status_result.get("googleCanonical")
            ),
            "user_canonical": optional_text(index_status_result.get("userCanonical")),
            "raw_response": (
                raw_response
                if raw_response
                else {
                    "error_code": result.error_code,
                    "error_message": result.error_message,
                }
            ),
        }

    async def _get_website_or_raise(self, website_id: UUID) -> Website:
        async with self._session_factory() as session:
            website = await session.get(Website, website_id)
            if website is None:
                raise ValueError(f"Website {website_id} does not exist")
            return website

    @staticmethod
    def _empty_result(
        *, website_id: UUID, requested_urls: int
    ) -> BatchProcessingResult:
        return BatchProcessingResult(
            website_id=website_id,
            requested_urls=requested_urls,
            dequeued_urls=0,
            submitted_urls=0,
            submission_success_count=0,
            submission_failure_count=0,
            inspected_urls=0,
            inspection_success_count=0,
            inspection_failure_count=0,
            requeued_urls=0,
            status=BatchProcessorStatus.COMPLETED,
            outcomes=[],
        )

    @staticmethod
    def _initial_outcomes(queued_urls: Sequence[URL]) -> dict[UUID, URLBatchOutcome]:
        return {
            queued_url.id: URLBatchOutcome(
                url_id=queued_url.id,
                url=queued_url.url,
                submission_success=False,
                submission_skipped=False,
                submission_error_code=None,
                submission_error_message=None,
                inspection_attempted=False,
                inspection_success=False,
                inspection_error_code=None,
                inspection_error_message=None,
            )
            for queued_url in queued_urls
        }

    @staticmethod
    def _submission_failure_result(
        *,
        url: str,
        action: SubmissionAction,
        error_code: str,
        error_message: str,
    ) -> IndexingURLResult:
        return IndexingURLResult(
            url=url,
            action=action.value,
            success=False,
            http_status=None,
            metadata=None,
            error_code=error_code,
            error_message=error_message,
            retry_after_seconds=None,
        )

    @staticmethod
    def _inspection_failure_result(
        *,
        inspection_url: str,
        site_url: str,
        error_code: str,
        error_message: str,
    ) -> IndexStatusResult:
        return IndexStatusResult(
            inspection_url=inspection_url,
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
            error_code=error_code,
            error_message=error_message,
            retry_after_seconds=None,
        )

    @staticmethod
    def _with_submission_result(
        outcome: URLBatchOutcome,
        result: IndexingURLResult,
    ) -> URLBatchOutcome:
        return URLBatchOutcome(
            url_id=outcome.url_id,
            url=outcome.url,
            submission_success=result.success,
            submission_skipped=False,
            submission_error_code=result.error_code,
            submission_error_message=result.error_message,
            inspection_attempted=outcome.inspection_attempted,
            inspection_success=outcome.inspection_success,
            inspection_error_code=outcome.inspection_error_code,
            inspection_error_message=outcome.inspection_error_message,
        )

    @staticmethod
    def _with_submission_failure(
        outcome: URLBatchOutcome,
        *,
        error_code: str,
        error_message: str,
    ) -> URLBatchOutcome:
        return URLBatchOutcome(
            url_id=outcome.url_id,
            url=outcome.url,
            submission_success=False,
            submission_skipped=False,
            submission_error_code=error_code,
            submission_error_message=error_message,
            inspection_attempted=False,
            inspection_success=False,
            inspection_error_code=None,
            inspection_error_message=None,
        )

    @staticmethod
    def _with_inspection_result(
        outcome: URLBatchOutcome,
        result: IndexStatusResult,
    ) -> URLBatchOutcome:
        return URLBatchOutcome(
            url_id=outcome.url_id,
            url=outcome.url,
            submission_success=outcome.submission_success,
            submission_skipped=outcome.submission_skipped,
            submission_error_code=outcome.submission_error_code,
            submission_error_message=outcome.submission_error_message,
            inspection_attempted=True,
            inspection_success=result.success,
            inspection_error_code=result.error_code,
            inspection_error_message=result.error_message,
        )

    @staticmethod
    def _with_submission_skipped(
        outcome: URLBatchOutcome,
        *,
        error_code: str,
        error_message: str,
    ) -> URLBatchOutcome:
        return URLBatchOutcome(
            url_id=outcome.url_id,
            url=outcome.url,
            submission_success=False,
            submission_skipped=True,
            submission_error_code=error_code,
            submission_error_message=error_message,
            inspection_attempted=outcome.inspection_attempted,
            inspection_success=outcome.inspection_success,
            inspection_error_code=outcome.inspection_error_code,
            inspection_error_message=outcome.inspection_error_message,
        )

    @staticmethod
    def _submission_status_from_result(result: IndexingURLResult) -> SubmissionStatus:
        if result.success:
            return SubmissionStatus.SUCCESS
        if result.error_code in {"QUOTA_EXCEEDED", "RATE_LIMITED"}:
            return SubmissionStatus.RATE_LIMITED
        return SubmissionStatus.FAILED

    @staticmethod
    def _submission_success_count(outcomes: Sequence[URLBatchOutcome]) -> int:
        return sum(1 for outcome in outcomes if outcome.submission_success)

    @staticmethod
    def _submission_skipped_count(outcomes: Sequence[URLBatchOutcome]) -> int:
        return sum(1 for outcome in outcomes if outcome.submission_skipped)

    @staticmethod
    def _inspection_success_count(outcomes: Sequence[URLBatchOutcome]) -> int:
        return sum(
            1
            for outcome in outcomes
            if outcome.inspection_attempted and outcome.inspection_success
        )

    @staticmethod
    def _derive_final_status(
        outcomes: Sequence[URLBatchOutcome],
    ) -> BatchProcessorStatus:
        if not outcomes:
            return BatchProcessorStatus.COMPLETED

        successful = [
            outcome
            for outcome in outcomes
            if outcome.submission_skipped
            or (
                outcome.submission_success
                and outcome.inspection_attempted
                and outcome.inspection_success
            )
        ]
        if len(successful) == len(outcomes):
            return BatchProcessorStatus.COMPLETED
        if successful:
            return BatchProcessorStatus.PARTIAL_FAILURE
        return BatchProcessorStatus.FAILED

    @staticmethod
    def _default_acquire_timeout_seconds() -> float:
        try:
            from seo_indexing_tracker.config import get_settings

            configured_timeout = getattr(
                get_settings(),
                "RATE_LIMIT_ACQUIRE_TIMEOUT_SECONDS",
                None,
            )
            if isinstance(configured_timeout, int | float) and configured_timeout > 0:
                return float(configured_timeout)
        except Exception:
            pass

        return DEFAULT_RATE_LIMIT_ACQUIRE_TIMEOUT_SECONDS

    async def _acquire_rate_limit_permit(
        self,
        *,
        website_id: UUID,
        api_type: str,
    ) -> RateLimitPermit:
        try:
            return await self._rate_limiter.acquire(
                website_id,
                api_type=api_type,
                timeout_seconds=self._rate_limit_acquire_timeout_seconds,
            )
        except TypeError as error:
            if "timeout_seconds" not in str(error):
                raise

            return await self._rate_limiter.acquire(website_id, api_type=api_type)

    @staticmethod
    def _should_requeue_outcome(outcome: URLBatchOutcome) -> bool:
        if outcome.submission_success or outcome.submission_skipped:
            return False

        # Submission failures are retry candidates, including timeout/rate-limit paths.
        return True

    async def _requeue_unfinished_urls(
        self,
        *,
        ordered_url_ids: Sequence[UUID],
        outcomes: dict[UUID, URLBatchOutcome],
        finalization_completed: bool,
    ) -> None:
        if finalization_completed:
            return

        unfinished_url_ids = [
            url_id
            for url_id in ordered_url_ids
            if self._should_requeue_outcome(outcomes[url_id])
        ]
        if not unfinished_url_ids:
            return

        try:
            await self._priority_queue.enqueue_many(unfinished_url_ids)
        except Exception:
            _batch_logger.exception(
                "failed_to_requeue_unfinished_urls",
                extra={
                    "unfinished_urls": len(unfinished_url_ids),
                },
            )

    @staticmethod
    def _chunk_urls(urls: Sequence[URL], chunk_size: int) -> list[list[URL]]:
        return [
            list(urls[index : index + chunk_size])
            for index in range(0, len(urls), chunk_size)
        ]

    async def _latest_index_statuses_by_url_ids(
        self,
        *,
        url_ids: Sequence[UUID],
    ) -> dict[UUID, IndexStatus]:
        if not url_ids:
            return {}

        latest_checked_subquery = (
            select(
                IndexStatus.url_id.label("url_id"),
                func.max(IndexStatus.checked_at).label("checked_at"),
            )
            .where(IndexStatus.url_id.in_(url_ids))
            .group_by(IndexStatus.url_id)
            .subquery()
        )

        async with self._session_factory() as session:
            statuses_result = await session.execute(
                select(IndexStatus)
                .join(
                    latest_checked_subquery,
                    and_(
                        IndexStatus.url_id == latest_checked_subquery.c.url_id,
                        IndexStatus.checked_at == latest_checked_subquery.c.checked_at,
                    ),
                )
                .where(IndexStatus.url_id.in_(url_ids))
            )
            latest_statuses = statuses_result.scalars().all()

        return {status.url_id: status for status in latest_statuses}

    @staticmethod
    def _is_already_indexed(index_status: IndexStatus | None) -> bool:
        if index_status is None:
            return False

        return index_status.coverage_state.strip().casefold() == "indexed"

    @staticmethod
    def _inspection_shows_indexed(result: IndexStatusResult | None) -> bool:
        """Check if inspection result indicates URL is already indexed."""
        if result is None:
            return False
        coverage_state = result.coverage_state
        if not coverage_state:
            return False

        # Use the same logic as derive_url_index_status_from_coverage_state
        from seo_indexing_tracker.models.url import URLIndexStatus

        derived = derive_url_index_status_from_coverage_state(coverage_state)
        return derived == URLIndexStatus.INDEXED

    async def _emit_progress(
        self,
        *,
        callback: ProgressCallback | None,
        website_id: UUID,
        stage: str,
        message: str,
        total_urls: int,
        processed_urls: int,
        successful_urls: int,
        failed_urls: int,
        checkpoint_data: dict[str, Any] | None = None,
    ) -> None:
        if callback is None:
            return

        update = BatchProgressUpdate(
            website_id=website_id,
            stage=stage,
            message=message,
            total_urls=total_urls,
            processed_urls=processed_urls,
            successful_urls=successful_urls,
            failed_urls=failed_urls,
            checkpoint_data=checkpoint_data,
        )
        try:
            maybe_awaitable = callback(update)
            if maybe_awaitable is None:
                return
            await maybe_awaitable
        except Exception:
            return

    async def _emit_checkpoint_if_due(
        self,
        *,
        callback: ProgressCallback | None,
        website_id: UUID,
        total_urls: int,
        processed_urls: int,
        successful_urls: int,
        failed_urls: int,
        next_checkpoint_at: int,
    ) -> int:
        checkpoint_interval = 100
        if processed_urls <= 0:
            return next_checkpoint_at

        updated_checkpoint_at = max(next_checkpoint_at, checkpoint_interval)
        while processed_urls >= updated_checkpoint_at:
            checkpoint_data = {
                "website_id": str(website_id),
                "processed_urls": updated_checkpoint_at,
                "total_urls": total_urls,
                "successful_urls": successful_urls,
                "failed_urls": failed_urls,
                "checkpoint_interval": checkpoint_interval,
            }
            _batch_logger.info(
                "batch_processing_checkpoint",
                extra=checkpoint_data,
            )
            await self._emit_progress(
                callback=callback,
                website_id=website_id,
                stage="checkpoint",
                message="Batch checkpoint reached",
                total_urls=total_urls,
                processed_urls=updated_checkpoint_at,
                successful_urls=successful_urls,
                failed_urls=failed_urls,
                checkpoint_data=checkpoint_data,
            )
            updated_checkpoint_at += checkpoint_interval

        return updated_checkpoint_at


__all__ = [
    "BatchProcessingResult",
    "BatchProcessorService",
    "BatchProcessorStatus",
    "BatchProgressUpdate",
    "DEFAULT_DEQUEUE_BATCH_SIZE",
    "DEFAULT_INSPECTION_BATCH_SIZE",
    "DEFAULT_SUBMIT_BATCH_SIZE",
    "URLBatchOutcome",
]
