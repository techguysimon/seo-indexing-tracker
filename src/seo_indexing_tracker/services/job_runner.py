"""Job execution runner with overlap protection and metrics tracking."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, cast
from uuid import UUID

from seo_indexing_tracker.database import session_scope
from seo_indexing_tracker.models import JobExecution
from seo_indexing_tracker.services.activity_service import ActivityService

_job_logger = logging.getLogger("seo_indexing_tracker.scheduler.jobs")


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


class JobRunnerService:
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
            except asyncio.CancelledError:
                metrics.failed_runs += 1
                metrics.last_error = "Job cancelled"
                _job_logger.warning(
                    "scheduler_pipeline_job_cancelled",
                    extra={"job_id": job_id},
                )
                await self._finish_job_execution(
                    execution_id=execution_id,
                    status="failed",
                    urls_processed=None,
                    checkpoint_data=None,
                    error_message="Job cancelled (timeout or shutdown)",
                )
                raise
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
            async with asyncio.timeout(900):
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

        async with asyncio.timeout(900):
            return await run(execution_id)


__all__ = ["JobExecutionMetrics", "JobRunResult", "JobRunnerService"]
