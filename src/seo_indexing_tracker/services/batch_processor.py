"""Batch processing service for URL dequeue, submit, and inspection workflows."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.models import (
    IndexStatus,
    IndexVerdict,
    SubmissionAction,
    SubmissionLog,
    SubmissionStatus,
    URL,
    Website,
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

DEFAULT_DEQUEUE_BATCH_SIZE = 100
DEFAULT_SUBMIT_BATCH_SIZE = 100
DEFAULT_INSPECTION_BATCH_SIZE = 25


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


@dataclass(slots=True, frozen=True)
class URLBatchOutcome:
    """Per-URL outcome for submission and inspection steps."""

    url_id: UUID
    url: str
    submission_success: bool
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
    ) -> BatchSubmitResult: ...


class _URLInspectionClient(Protocol):
    async def inspect_url(self, url: str, site_url: str) -> IndexStatusResult: ...


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
            requeued_count = await self._priority_queue.enqueue_many(ordered_url_ids)
            return BatchProcessingResult(
                website_id=website_id,
                requested_urls=dequeue_limit,
                dequeued_urls=len(queued_urls),
                submitted_urls=len(queued_urls),
                submission_success_count=0,
                submission_failure_count=len(queued_urls),
                inspected_urls=0,
                inspection_success_count=0,
                inspection_failure_count=0,
                requeued_urls=requeued_count,
                status=BatchProcessorStatus.FAILED,
                outcomes=[failure_outcomes[url_id] for url_id in ordered_url_ids],
            )

        submitted_results: dict[UUID, IndexingURLResult] = {}
        submission_logs: list[_SubmissionLogRecord] = []

        for url_batch in self._chunk_urls(queued_urls, self._submit_batch_size):
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
                processed_urls=len(submitted_results),
                successful_urls=self._submission_success_count(list(outcomes.values())),
                failed_urls=len(submitted_results)
                - self._submission_success_count(list(outcomes.values())),
            )

        await self._record_submission_logs(action=action, logs=submission_logs)

        successfully_submitted = [
            queued_url
            for queued_url in queued_urls
            if submitted_results.get(queued_url.id, None) is not None
            and submitted_results[queued_url.id].success
        ]

        inspection_records: list[_InspectionRecord] = []
        inspected_count = 0
        for inspect_batch in self._chunk_urls(
            successfully_submitted,
            self._inspection_batch_size,
        ):
            inspection_results = await self._inspect_url_batch(
                website_id=website_id,
                site_url=website.site_url,
                urls=inspect_batch,
                client=clients.search_console,
            )
            for inspected_url in inspect_batch:
                inspection_result = inspection_results[inspected_url.id]
                outcomes[inspected_url.id] = self._with_inspection_result(
                    outcomes[inspected_url.id],
                    inspection_result,
                )
                inspection_records.append(
                    _InspectionRecord(url_id=inspected_url.id, result=inspection_result)
                )

            inspected_count += len(inspect_batch)
            await self._emit_progress(
                callback=progress_callback,
                website_id=website_id,
                stage="inspect",
                message="Processed URL inspection batch",
                total_urls=len(successfully_submitted),
                processed_urls=inspected_count,
                successful_urls=self._inspection_success_count(list(outcomes.values())),
                failed_urls=inspected_count
                - self._inspection_success_count(list(outcomes.values())),
            )

        await self._record_index_statuses(records=inspection_records)

        failed_url_ids = [
            outcome.url_id
            for outcome in outcomes.values()
            if (not outcome.submission_success)
            or (outcome.inspection_attempted and not outcome.inspection_success)
        ]
        requeued_count = await self._priority_queue.enqueue_many(failed_url_ids)

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
            submission_failure_count=len(submitted_results) - submission_success_count,
            inspected_urls=len(inspected_outcomes),
            inspection_success_count=inspection_success_count,
            inspection_failure_count=len(inspected_outcomes) - inspection_success_count,
            requeued_urls=requeued_count,
            status=status,
            outcomes=final_outcomes,
        )

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

        for url in urls:
            try:
                permit = await self._rate_limiter.acquire(
                    website_id, api_type="indexing"
                )
            except Exception as error:
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
            return results, logs

        try:
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

            results[url.id] = submission_result
            logs.append(
                _SubmissionLogRecord(
                    url_id=url.id,
                    api_response=asdict(submission_result),
                    status=self._submission_status_from_result(submission_result),
                    error_message=submission_result.error_message,
                )
            )

        return results, logs

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
            permit = await self._rate_limiter.acquire(website_id, api_type="inspection")
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

    def _index_status_row(
        self,
        *,
        record: _InspectionRecord,
        checked_at: datetime,
    ) -> dict[str, Any]:
        result = record.result
        raw_response = result.raw_response or {}
        index_status_result = self._extract_index_status_result(raw_response)

        return {
            "url_id": record.url_id,
            "coverage_state": result.coverage_state or "INSPECTION_FAILED",
            "verdict": self._index_verdict(result.verdict),
            "last_crawl_time": result.last_crawl_time,
            "indexed_at": result.last_crawl_time,
            "checked_at": checked_at,
            "robots_txt_state": result.robots_txt_state,
            "indexing_state": result.indexing_state,
            "page_fetch_state": self._optional_text(
                index_status_result.get("pageFetchState")
            ),
            "google_canonical": self._optional_text(
                index_status_result.get("googleCanonical")
            ),
            "user_canonical": self._optional_text(
                index_status_result.get("userCanonical")
            ),
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
            submission_error_code=outcome.submission_error_code,
            submission_error_message=outcome.submission_error_message,
            inspection_attempted=True,
            inspection_success=result.success,
            inspection_error_code=result.error_code,
            inspection_error_message=result.error_message,
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
    def _inspection_success_count(outcomes: Sequence[URLBatchOutcome]) -> int:
        return sum(
            1
            for outcome in outcomes
            if outcome.inspection_attempted and outcome.inspection_success
        )

    @staticmethod
    def _index_verdict(verdict: str | None) -> IndexVerdict:
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

    @staticmethod
    def _extract_index_status_result(raw_response: dict[str, Any]) -> dict[str, Any]:
        inspection_result = raw_response.get("inspectionResult")
        if not isinstance(inspection_result, dict):
            return {}

        index_status_result = inspection_result.get("indexStatusResult")
        if not isinstance(index_status_result, dict):
            return {}

        return index_status_result

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped if stripped else None
        return None

    @staticmethod
    def _derive_final_status(
        outcomes: Sequence[URLBatchOutcome],
    ) -> BatchProcessorStatus:
        if not outcomes:
            return BatchProcessorStatus.COMPLETED

        successful = [
            outcome
            for outcome in outcomes
            if outcome.submission_success
            and outcome.inspection_attempted
            and outcome.inspection_success
        ]
        if len(successful) == len(outcomes):
            return BatchProcessorStatus.COMPLETED
        if successful:
            return BatchProcessorStatus.PARTIAL_FAILURE
        return BatchProcessorStatus.FAILED

    @staticmethod
    def _chunk_urls(urls: Sequence[URL], chunk_size: int) -> list[list[URL]]:
        return [
            list(urls[index : index + chunk_size])
            for index in range(0, len(urls), chunk_size)
        ]

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
        )
        try:
            maybe_awaitable = callback(update)
            if maybe_awaitable is None:
                return
            await maybe_awaitable
        except Exception:
            return


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
