"""Scheduler control API routes."""

from __future__ import annotations

from datetime import datetime
from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.models import JobExecution
from seo_indexing_tracker.services.processing_pipeline import (
    JobExecutionMetrics,
    SchedulerProcessingPipelineService,
)
from seo_indexing_tracker.services.scheduler import SchedulerJobState, SchedulerService

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


class SchedulerStatusResponse(BaseModel):
    """Runtime scheduler status response."""

    enabled: bool
    running: bool
    paused: bool


class SchedulerJobResponse(BaseModel):
    """Scheduler job response payload."""

    job_id: str
    name: str | None
    trigger: str
    next_run_time: datetime | None
    paused: bool


class SchedulerJobMonitoringResponse(BaseModel):
    """Scheduler pipeline job runtime metrics."""

    job_id: str
    name: str
    total_runs: int
    successful_runs: int
    failed_runs: int
    overlap_skips: int
    running: bool
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_duration_ms: float | None
    last_error: str | None


class JobExecutionHistoryItem(BaseModel):
    """Persisted scheduler job execution item."""

    job_id: str
    job_name: str
    website_id: UUID | None
    started_at: datetime
    finished_at: datetime | None
    status: str
    urls_processed: int
    error_message: str | None


class JobExecutionHistoryResponse(BaseModel):
    """Paginated scheduler job execution history payload."""

    page: int
    page_size: int
    total_items: int
    total_pages: int
    items: list[JobExecutionHistoryItem]


def _get_scheduler_service(request: Request) -> SchedulerService:
    scheduler = getattr(request.app.state, "scheduler_service", None)
    if isinstance(scheduler, SchedulerService):
        return scheduler

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Scheduler service is unavailable",
    )


def _get_processing_pipeline_service(
    request: Request,
) -> SchedulerProcessingPipelineService:
    pipeline_service = getattr(request.app.state, "processing_pipeline_service", None)
    if isinstance(pipeline_service, SchedulerProcessingPipelineService):
        return pipeline_service

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Processing pipeline service is unavailable",
    )


def _job_response_from_state(job_state: SchedulerJobState) -> SchedulerJobResponse:
    return SchedulerJobResponse(
        job_id=job_state.job_id,
        name=job_state.name,
        trigger=job_state.trigger,
        next_run_time=job_state.next_run_time,
        paused=job_state.paused,
    )


def _job_monitoring_response(
    metrics: JobExecutionMetrics,
) -> SchedulerJobMonitoringResponse:
    return SchedulerJobMonitoringResponse(
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


def _raise_scheduler_error(error: Exception) -> NoReturn:
    if isinstance(error, LookupError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error

    if isinstance(error, RuntimeError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Unexpected scheduler operation failure",
    ) from error


@router.get("", response_model=SchedulerStatusResponse, status_code=status.HTTP_200_OK)
async def get_scheduler_status(
    scheduler: SchedulerService = Depends(_get_scheduler_service),
) -> SchedulerStatusResponse:
    return SchedulerStatusResponse(
        enabled=scheduler.enabled,
        running=scheduler.running,
        paused=scheduler.paused,
    )


@router.post(
    "/pause", response_model=SchedulerStatusResponse, status_code=status.HTTP_200_OK
)
async def pause_scheduler(
    scheduler: SchedulerService = Depends(_get_scheduler_service),
) -> SchedulerStatusResponse:
    try:
        scheduler.pause()
    except Exception as error:
        _raise_scheduler_error(error)

    return SchedulerStatusResponse(
        enabled=scheduler.enabled,
        running=scheduler.running,
        paused=scheduler.paused,
    )


@router.post(
    "/resume", response_model=SchedulerStatusResponse, status_code=status.HTTP_200_OK
)
async def resume_scheduler(
    scheduler: SchedulerService = Depends(_get_scheduler_service),
) -> SchedulerStatusResponse:
    try:
        scheduler.resume()
    except Exception as error:
        _raise_scheduler_error(error)

    return SchedulerStatusResponse(
        enabled=scheduler.enabled,
        running=scheduler.running,
        paused=scheduler.paused,
    )


@router.get(
    "/jobs",
    response_model=list[SchedulerJobResponse],
    status_code=status.HTTP_200_OK,
)
async def list_scheduler_jobs(
    scheduler: SchedulerService = Depends(_get_scheduler_service),
) -> list[SchedulerJobResponse]:
    jobs: list[SchedulerJobState] = []
    try:
        jobs = scheduler.list_jobs()
    except Exception as error:
        _raise_scheduler_error(error)

    return [_job_response_from_state(job) for job in jobs]


@router.get(
    "/jobs/monitoring",
    response_model=list[SchedulerJobMonitoringResponse],
    status_code=status.HTTP_200_OK,
)
async def list_scheduler_job_monitoring(
    pipeline_service: SchedulerProcessingPipelineService = Depends(
        _get_processing_pipeline_service
    ),
) -> list[SchedulerJobMonitoringResponse]:
    return [
        _job_monitoring_response(metrics)
        for metrics in pipeline_service.monitoring_snapshot()
    ]


@router.get(
    "/jobs/history",
    response_model=JobExecutionHistoryResponse,
    status_code=status.HTTP_200_OK,
)
async def list_scheduler_job_history(
    page: int = 1,
    page_size: int = 20,
    job_id: str | None = None,
    website_id: UUID | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> JobExecutionHistoryResponse:
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)

    statement = select(JobExecution)
    if job_id:
        statement = statement.where(JobExecution.job_id == job_id.strip())
    if website_id is not None:
        statement = statement.where(JobExecution.website_id == website_id)
    if status_filter:
        statement = statement.where(
            JobExecution.status == status_filter.strip().lower()
        )
    if date_from is not None:
        statement = statement.where(JobExecution.started_at >= date_from)
    if date_to is not None:
        statement = statement.where(JobExecution.started_at <= date_to)

    total_items = int(
        (await session.scalar(select(func.count()).select_from(statement.subquery())))
        or 0
    )
    total_pages = max(1, ((total_items - 1) // safe_page_size) + 1)
    bounded_page = min(safe_page, total_pages)

    rows = (
        (
            await session.execute(
                statement.order_by(JobExecution.started_at.desc())
                .offset((bounded_page - 1) * safe_page_size)
                .limit(safe_page_size)
            )
        )
        .scalars()
        .all()
    )

    return JobExecutionHistoryResponse(
        page=bounded_page,
        page_size=safe_page_size,
        total_items=total_items,
        total_pages=total_pages,
        items=[
            JobExecutionHistoryItem(
                job_id=row.job_id,
                job_name=row.job_name,
                website_id=row.website_id,
                started_at=row.started_at,
                finished_at=row.finished_at,
                status=row.status,
                urls_processed=row.urls_processed,
                error_message=row.error_message,
            )
            for row in rows
        ],
    )


@router.post(
    "/jobs/{job_id}/pause",
    response_model=SchedulerJobResponse,
    status_code=status.HTTP_200_OK,
)
async def pause_scheduler_job(
    job_id: str,
    scheduler: SchedulerService = Depends(_get_scheduler_service),
) -> SchedulerJobResponse:
    job_state = SchedulerJobState(
        job_id=job_id,
        name=None,
        trigger="unknown",
        next_run_time=None,
        paused=False,
    )
    try:
        job_state = scheduler.pause_job(job_id)
    except Exception as error:
        _raise_scheduler_error(error)

    return _job_response_from_state(job_state)


@router.post(
    "/jobs/{job_id}/resume",
    response_model=SchedulerJobResponse,
    status_code=status.HTTP_200_OK,
)
async def resume_scheduler_job(
    job_id: str,
    scheduler: SchedulerService = Depends(_get_scheduler_service),
) -> SchedulerJobResponse:
    job_state = SchedulerJobState(
        job_id=job_id,
        name=None,
        trigger="unknown",
        next_run_time=None,
        paused=False,
    )
    try:
        job_state = scheduler.resume_job(job_id)
    except Exception as error:
        _raise_scheduler_error(error)

    return _job_response_from_state(job_state)
