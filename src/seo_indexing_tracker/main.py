"""Application entry point for SEO Indexing Tracker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import logging
from pathlib import Path
import signal
from typing import Any

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from starlette.responses import Response
import uvicorn

from seo_indexing_tracker.api.activity import router as activity_router
from seo_indexing_tracker.api.config_validation import (
    router as config_validation_router,
)
from seo_indexing_tracker.api.index_stats import router as index_stats_router
from seo_indexing_tracker.api.quota import router as quota_router
from seo_indexing_tracker.api.queue import router as queue_router
from seo_indexing_tracker.api.scheduler import router as scheduler_router
from seo_indexing_tracker.api.service_accounts import router as service_accounts_router
from seo_indexing_tracker.api.sitemap_progress import router as sitemap_progress_router
from seo_indexing_tracker.api.sitemaps import router as sitemaps_router
from seo_indexing_tracker.api.urls import router as urls_router
from seo_indexing_tracker.api.web import router as web_router
from seo_indexing_tracker.api.websites import router as websites_router
from seo_indexing_tracker.config import get_settings
from seo_indexing_tracker.database import (
    close_database,
    initialize_database,
    run_startup_database_health_check,
    session_scope,
)
from seo_indexing_tracker.models import JobExecution, QuotaUsage, URL
from seo_indexing_tracker.services.job_recovery_service import JobRecoveryService
from seo_indexing_tracker.services.processing_pipeline import (
    SchedulerProcessingPipelineService,
    set_scheduler_processing_pipeline_service,
)
from seo_indexing_tracker.services.scheduler import SchedulerService
from seo_indexing_tracker.utils.logging import (
    add_request_logging_middleware,
    setup_logging,
)

__all__ = ["app", "create_app", "main"]

_lifecycle_logger = logging.getLogger("seo_indexing_tracker.lifecycle")


def _initialize_lifecycle_state(app: FastAPI) -> None:
    app.state.inflight_requests = 0
    app.state.requests_drained = asyncio.Event()
    app.state.requests_drained.set()
    app.state.shutdown_requested = asyncio.Event()
    app.state.shutdown_signal = None
    app.state.session_started_at = datetime.now(UTC)


def _handle_shutdown_signal(app: FastAPI, signum: int) -> None:
    if app.state.shutdown_requested.is_set():
        return

    app.state.shutdown_signal = signal.Signals(signum).name
    app.state.shutdown_requested.set()
    _lifecycle_logger.warning(
        "shutdown_signal_received",
        extra={"signal": app.state.shutdown_signal},
    )


async def _log_startup_recovery_summary(
    *,
    interrupted_jobs_detected: int,
    auto_resumed_jobs: int,
) -> None:
    async with session_scope() as session:
        status_rows = (
            await session.execute(
                select(URL.latest_index_status, func.count(URL.id))
                .group_by(URL.latest_index_status)
                .order_by(URL.latest_index_status.asc())
            )
        ).all()
        url_status_counts = {
            getattr(row[0], "value", str(row[0])): int(row[1]) for row in status_rows
        }
        pending_queue_size = int(
            (
                await session.execute(
                    select(func.count(URL.id)).where(URL.current_priority > 0)
                )
            ).scalar_one()
        )
        quota_usage_totals = (
            await session.execute(
                select(
                    func.coalesce(func.sum(QuotaUsage.indexing_count), 0),
                    func.coalesce(func.sum(QuotaUsage.inspection_count), 0),
                ).where(QuotaUsage.date == datetime.now(UTC).date())
            )
        ).one()
        running_jobs_after_recovery = int(
            (
                await session.execute(
                    select(func.count(JobExecution.id)).where(
                        JobExecution.status == "running"
                    )
                )
            ).scalar_one()
        )

    _lifecycle_logger.info(
        "startup_recovery_summary",
        extra={
            "url_status_counts": url_status_counts,
            "pending_queue_size": pending_queue_size,
            "current_quota_usage": {
                "indexing": int(quota_usage_totals[0]),
                "inspection": int(quota_usage_totals[1]),
            },
            "interrupted_jobs_detected": interrupted_jobs_detected,
            "auto_resumed_jobs": auto_resumed_jobs,
            "running_jobs_after_recovery": running_jobs_after_recovery,
        },
    )


async def _wait_for_inflight_requests(app: FastAPI, *, timeout_seconds: int) -> bool:
    if app.state.inflight_requests <= 0:
        return True

    try:
        await asyncio.wait_for(
            app.state.requests_drained.wait(), timeout=timeout_seconds
        )
    except TimeoutError:
        return False

    return True


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _initialize_lifecycle_state(app)

    scheduler_service = SchedulerService.from_settings(settings)
    recovery_service = JobRecoveryService()
    processing_pipeline_service = SchedulerProcessingPipelineService(
        scheduler=scheduler_service,
        settings=settings,
    )
    set_scheduler_processing_pipeline_service(processing_pipeline_service)
    app.state.scheduler_service = scheduler_service
    app.state.recovery_service = recovery_service
    app.state.processing_pipeline_service = processing_pipeline_service

    previous_handlers: dict[signal.Signals, Any] = {}
    for handled_signal in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[handled_signal] = signal.getsignal(handled_signal)

        def _signal_handler(signum: int, frame: object | None) -> None:
            _handle_shutdown_signal(app, signum)
            previous_handler = previous_handlers[signal.Signals(signum)]
            if callable(previous_handler):
                previous_handler(signum, frame)

        signal.signal(handled_signal, _signal_handler)

    await initialize_database()
    await run_startup_database_health_check()
    startup_recovery_result = await recovery_service.handle_startup_recovery(
        auto_resume=settings.JOB_RECOVERY_AUTO_RESUME
    )
    await _log_startup_recovery_summary(
        interrupted_jobs_detected=startup_recovery_result.detected_count,
        auto_resumed_jobs=startup_recovery_result.auto_resumed,
    )
    processing_pipeline_service.register_jobs()
    await scheduler_service.start()

    try:
        yield
    finally:
        graceful_shutdown = await _wait_for_inflight_requests(
            app,
            timeout_seconds=settings.SHUTDOWN_GRACE_PERIOD_SECONDS,
        )
        await scheduler_service.shutdown()
        jobs_marked_interrupted = await recovery_service.persist_shutdown_checkpoints()
        shutdown_summary = await recovery_service.summarize_session(
            session_started_at=app.state.session_started_at
        )
        _lifecycle_logger.info(
            "shutdown_summary",
            extra={
                "jobs_completed": shutdown_summary.jobs_completed,
                "jobs_interrupted": shutdown_summary.jobs_interrupted,
                "urls_processed": shutdown_summary.urls_processed,
                "jobs_marked_interrupted": jobs_marked_interrupted,
                "graceful_shutdown": graceful_shutdown,
                "forced_timeout": not graceful_shutdown,
                "inflight_requests": app.state.inflight_requests,
                "signal": app.state.shutdown_signal,
            },
        )
        for handled_signal, previous_handler in previous_handlers.items():
            signal.signal(handled_signal, previous_handler)
        await close_database()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings)

    package_directory = Path(__file__).resolve().parent
    templates = Jinja2Templates(
        directory=str(package_directory / "templates"),
        context_processors=[
            lambda request: {
                "current_user": getattr(request.state, "current_user", None)
            },
            lambda request: {"settings": request.app.state.settings},
        ],
    )

    app = FastAPI(title="SEO Indexing Tracker", lifespan=lifespan)
    _initialize_lifecycle_state(app)

    @app.middleware("http")
    async def track_inflight_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        app.state.inflight_requests = (
            int(getattr(app.state, "inflight_requests", 0)) + 1
        )
        requests_drained = getattr(app.state, "requests_drained", None)
        if requests_drained is None:
            requests_drained = asyncio.Event()
            app.state.requests_drained = requests_drained
        requests_drained.clear()
        try:
            response = await call_next(request)
        finally:
            app.state.inflight_requests = max(0, app.state.inflight_requests - 1)
            if app.state.inflight_requests == 0:
                requests_drained.set()
        return response

    app.state.settings = settings
    app.state.templates = templates
    add_request_logging_middleware(app)
    app.mount(
        "/static",
        StaticFiles(directory=str(package_directory / "static")),
        name="static",
    )
    app.include_router(config_validation_router)
    app.include_router(web_router)
    app.include_router(queue_router)
    app.include_router(scheduler_router)
    app.include_router(activity_router)
    app.include_router(index_stats_router)
    app.include_router(quota_router)
    app.include_router(urls_router)
    app.include_router(websites_router)
    app.include_router(service_accounts_router)
    app.include_router(sitemaps_router)
    app.include_router(sitemap_progress_router)

    @app.get("/health", tags=["health"])
    async def health_check() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "seo_indexing_tracker.main:app",
        host=settings.HOST,
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(),
        reload=False,
    )
