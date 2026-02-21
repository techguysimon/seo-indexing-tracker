"""Startup and shutdown recovery helpers for interrupted job executions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.models import JobExecution

SessionScopeFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_recovery_logger = logging.getLogger("seo_indexing_tracker.recovery")


@dataclass(slots=True, frozen=True)
class InterruptedJobRecord:
    """Serializable interrupted job details for logs and lifecycle summaries."""

    execution_id: UUID
    job_id: str
    job_name: str
    started_at: datetime
    urls_processed: int
    checkpoint_data: dict[str, Any] | None


@dataclass(slots=True, frozen=True)
class StartupRecoveryResult:
    """Startup recovery outcome details used by boot logs."""

    detected_jobs: tuple[InterruptedJobRecord, ...]
    auto_resumed: int

    @property
    def detected_count(self) -> int:
        return len(self.detected_jobs)


@dataclass(slots=True, frozen=True)
class ShutdownExecutionSummary:
    """Session-scoped shutdown aggregate counters."""

    jobs_completed: int
    jobs_interrupted: int
    urls_processed: int


class JobRecoveryService:
    """Manage interrupted job execution detection and crash recovery updates."""

    def __init__(self, *, session_factory: SessionScopeFactory | None = None) -> None:
        if session_factory is None:
            from seo_indexing_tracker.database import session_scope

            session_factory = session_scope

        self._session_factory = session_factory

    async def handle_startup_recovery(
        self,
        *,
        auto_resume: bool = False,
    ) -> StartupRecoveryResult:
        """Detect and recover running jobs left unfinished by prior shutdown."""

        interrupted_jobs = await self._list_running_jobs()
        if interrupted_jobs:
            _recovery_logger.warning(
                "startup_interrupted_jobs_detected",
                extra={
                    "count": len(interrupted_jobs),
                    "job_ids": [job.job_id for job in interrupted_jobs],
                    "execution_ids": [
                        str(job.execution_id) for job in interrupted_jobs
                    ],
                },
            )
        else:
            _recovery_logger.info("startup_interrupted_jobs_not_found")

        await self._mark_running_jobs_failed(
            stage="startup_recovery",
            reason="Recovered unfinished job after process interruption",
        )

        auto_resumed = 0
        if auto_resume:
            _recovery_logger.warning(
                "startup_auto_resume_not_supported",
                extra={
                    "detected_jobs": len(interrupted_jobs),
                },
            )

        return StartupRecoveryResult(
            detected_jobs=tuple(interrupted_jobs),
            auto_resumed=auto_resumed,
        )

    async def persist_shutdown_checkpoints(self) -> int:
        """Persist checkpoint and failure metadata for running jobs on shutdown."""

        return await self._mark_running_jobs_failed(
            stage="shutdown",
            reason="Job interrupted by application shutdown",
        )

    async def summarize_session(
        self,
        *,
        session_started_at: datetime,
    ) -> ShutdownExecutionSummary:
        """Return session execution totals for shutdown logs."""

        async with self._session_factory() as session:
            executions = (
                (
                    await session.execute(
                        select(JobExecution).where(
                            JobExecution.started_at >= session_started_at
                        )
                    )
                )
                .scalars()
                .all()
            )

        jobs_completed = sum(
            1 for execution in executions if execution.status == "success"
        )
        jobs_interrupted = sum(
            1 for execution in executions if execution.status == "failed"
        )
        urls_processed = sum(int(execution.urls_processed) for execution in executions)
        return ShutdownExecutionSummary(
            jobs_completed=jobs_completed,
            jobs_interrupted=jobs_interrupted,
            urls_processed=urls_processed,
        )

    async def _list_running_jobs(self) -> list[InterruptedJobRecord]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(JobExecution)
                        .where(JobExecution.status == "running")
                        .order_by(JobExecution.started_at.asc())
                    )
                )
                .scalars()
                .all()
            )

        return [
            InterruptedJobRecord(
                execution_id=row.id,
                job_id=row.job_id,
                job_name=row.job_name,
                started_at=row.started_at,
                urls_processed=int(row.urls_processed),
                checkpoint_data=row.checkpoint_data,
            )
            for row in rows
        ]

    async def _mark_running_jobs_failed(self, *, stage: str, reason: str) -> int:
        interrupted_at = datetime.now(UTC)
        updated_count = 0
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(JobExecution).where(JobExecution.status == "running")
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.status = "failed"
                row.finished_at = interrupted_at
                row.error_message = reason
                base_checkpoint = (
                    dict(row.checkpoint_data)
                    if isinstance(row.checkpoint_data, dict)
                    else {}
                )
                base_checkpoint["stage"] = stage
                base_checkpoint["interrupted_at"] = interrupted_at.isoformat()
                base_checkpoint["recovery_reason"] = reason
                row.checkpoint_data = base_checkpoint
                updated_count += 1

        if updated_count > 0:
            _recovery_logger.info(
                "running_jobs_marked_failed",
                extra={"count": updated_count, "stage": stage},
            )
        return updated_count


__all__ = [
    "InterruptedJobRecord",
    "JobRecoveryService",
    "ShutdownExecutionSummary",
    "StartupRecoveryResult",
]
