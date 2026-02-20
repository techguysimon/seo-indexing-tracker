"""APScheduler integration service with lifecycle-safe controls."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from apscheduler.events import EVENT_JOB_ERROR  # type: ignore[import-untyped]
from apscheduler.events import EVENT_JOB_EXECUTED
from apscheduler.events import EVENT_JOB_SUBMITTED
from apscheduler.events import JobExecutionEvent
from apscheduler.events import JobSubmissionEvent
from apscheduler.events import SchedulerEvent
from apscheduler.job import Job  # type: ignore[import-untyped]
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore  # type: ignore[import-untyped]
from apscheduler.schedulers.base import STATE_PAUSED  # type: ignore[import-untyped]
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]

from seo_indexing_tracker.config import Settings

JobCallable = Callable[[], Awaitable[None] | None]

_scheduler_logger = logging.getLogger("seo_indexing_tracker.scheduler")


@dataclass(slots=True, frozen=True)
class SchedulerJobState:
    """Serializable scheduler job state for API responses."""

    job_id: str
    name: str | None
    trigger: str
    next_run_time: datetime | None
    paused: bool


class SchedulerService:
    """Encapsulate scheduler startup, job management, and event logging."""

    def __init__(
        self,
        *,
        enabled: bool,
        jobstore_url: str,
        scheduler: AsyncIOScheduler | None = None,
    ) -> None:
        self._enabled = enabled
        self._scheduler = scheduler or AsyncIOScheduler(
            jobstores={"default": SQLAlchemyJobStore(url=jobstore_url)}
        )
        self._scheduler.add_listener(
            self._handle_job_event,
            EVENT_JOB_SUBMITTED | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> SchedulerService:
        return cls(
            enabled=settings.SCHEDULER_ENABLED,
            jobstore_url=settings.SCHEDULER_JOBSTORE_URL,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def running(self) -> bool:
        if not self._enabled:
            return False
        return cast(bool, self._scheduler.running)

    @property
    def paused(self) -> bool:
        if not self._enabled:
            return False
        return cast(bool, self._scheduler.state == STATE_PAUSED)

    async def start(self) -> None:
        if not self._enabled:
            _scheduler_logger.info("scheduler_disabled")
            return

        if self._scheduler.running:
            return

        self._scheduler.start()
        _scheduler_logger.info("scheduler_started")

    async def shutdown(self) -> None:
        if not self._enabled:
            return

        if not self._scheduler.running:
            return

        self._scheduler.shutdown(wait=False)
        _scheduler_logger.info("scheduler_shutdown")

    def pause(self) -> None:
        self._ensure_enabled()
        if not self._scheduler.running:
            raise RuntimeError("Scheduler is not running")
        self._scheduler.pause()
        _scheduler_logger.info("scheduler_paused")

    def resume(self) -> None:
        self._ensure_enabled()
        if not self._scheduler.running:
            raise RuntimeError("Scheduler is not running")
        self._scheduler.resume()
        _scheduler_logger.info("scheduler_resumed")

    def add_interval_job(
        self,
        *,
        job_id: str,
        func: JobCallable,
        seconds: int,
        name: str | None = None,
        replace_existing: bool = True,
    ) -> Job:
        self._ensure_enabled()
        if seconds <= 0:
            raise ValueError("Interval seconds must be greater than zero")

        return self._scheduler.add_job(
            func=func,
            trigger="interval",
            seconds=seconds,
            id=job_id,
            name=name,
            replace_existing=replace_existing,
        )

    def add_cron_job(
        self,
        *,
        job_id: str,
        func: JobCallable,
        minute: str = "*",
        hour: str = "*",
        day: str = "*",
        day_of_week: str = "*",
        month: str = "*",
        name: str | None = None,
        replace_existing: bool = True,
    ) -> Job:
        self._ensure_enabled()

        return self._scheduler.add_job(
            func=func,
            trigger="cron",
            minute=minute,
            hour=hour,
            day=day,
            day_of_week=day_of_week,
            month=month,
            id=job_id,
            name=name,
            replace_existing=replace_existing,
        )

    def pause_job(self, job_id: str) -> SchedulerJobState:
        self._ensure_enabled()
        job = self._require_job(job_id)
        self._scheduler.pause_job(job.id)
        _scheduler_logger.info("scheduler_job_paused", extra={"job_id": job.id})
        return self._job_to_state(self._require_job(job_id))

    def resume_job(self, job_id: str) -> SchedulerJobState:
        self._ensure_enabled()
        job = self._require_job(job_id)
        self._scheduler.resume_job(job.id)
        _scheduler_logger.info("scheduler_job_resumed", extra={"job_id": job.id})
        return self._job_to_state(self._require_job(job_id))

    def list_jobs(self) -> list[SchedulerJobState]:
        self._ensure_enabled()
        return [self._job_to_state(job) for job in self._scheduler.get_jobs()]

    def _ensure_enabled(self) -> None:
        if self._enabled:
            return
        raise RuntimeError("Scheduler is disabled")

    def _require_job(self, job_id: str) -> Job:
        job = self._scheduler.get_job(job_id)
        if job is not None:
            return job
        raise LookupError(f"Scheduler job '{job_id}' not found")

    @staticmethod
    def _job_to_state(job: Job) -> SchedulerJobState:
        return SchedulerJobState(
            job_id=job.id,
            name=job.name,
            trigger=str(job.trigger),
            next_run_time=job.next_run_time,
            paused=job.next_run_time is None,
        )

    @staticmethod
    def _scheduled_times_iso(
        run_times: tuple[datetime, ...] | list[datetime],
    ) -> list[str]:
        if not run_times:
            return []
        return [run_time.isoformat() for run_time in run_times]

    @staticmethod
    def _handle_job_event(event: SchedulerEvent) -> None:
        if isinstance(event, JobSubmissionEvent):
            _scheduler_logger.info(
                "scheduler_job_started",
                extra={
                    "job_id": event.job_id,
                    "scheduled_run_times": SchedulerService._scheduled_times_iso(
                        event.scheduled_run_times
                    ),
                },
            )
            return

        if not isinstance(event, JobExecutionEvent):
            return

        if event.exception is None:
            _scheduler_logger.info(
                "scheduler_job_succeeded",
                extra={"job_id": event.job_id},
            )
            return

        _scheduler_logger.error(
            "scheduler_job_failed",
            extra={
                "job_id": event.job_id,
                "exception": str(event.exception),
                "traceback": event.traceback,
            },
        )


__all__ = ["SchedulerJobState", "SchedulerService"]
