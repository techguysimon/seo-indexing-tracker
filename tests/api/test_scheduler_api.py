"""Tests for scheduler management API routes."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.sqlite")
os.environ.setdefault("SECRET_KEY", "test-secret")

from seo_indexing_tracker.config import get_settings
from seo_indexing_tracker.api.scheduler import router
from seo_indexing_tracker.services.processing_pipeline import (
    INDEX_VERIFICATION_JOB_ID,
    SITEMAP_REFRESH_JOB_ID,
    URL_SUBMISSION_JOB_ID,
    SchedulerProcessingPipelineService,
)
from seo_indexing_tracker.services.scheduler import SchedulerService


async def _noop_job() -> None:
    return None


@pytest.mark.asyncio
async def test_scheduler_api_pause_resume_and_job_controls(tmp_path: Path) -> None:
    scheduler = SchedulerService(
        enabled=True,
        jobstore_url=f"sqlite:///{tmp_path / 'scheduler-api.sqlite'}",
    )
    scheduler.add_interval_job(job_id="api-job", func=_noop_job, seconds=300)

    app = FastAPI()
    app.include_router(router)
    app.state.scheduler_service = scheduler

    await scheduler.start()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            status_response = await client.get("/api/scheduler")
            assert status_response.status_code == 200
            assert status_response.json()["enabled"] is True
            assert status_response.json()["running"] is True

            jobs_response = await client.get("/api/scheduler/jobs")
            assert jobs_response.status_code == 200
            assert len(jobs_response.json()) == 1
            assert jobs_response.json()[0]["job_id"] == "api-job"

            pause_response = await client.post("/api/scheduler/pause")
            assert pause_response.status_code == 200
            assert pause_response.json()["paused"] is True

            resume_response = await client.post("/api/scheduler/resume")
            assert resume_response.status_code == 200
            assert resume_response.json()["paused"] is False

            pause_job_response = await client.post("/api/scheduler/jobs/api-job/pause")
            assert pause_job_response.status_code == 200
            assert pause_job_response.json()["paused"] is True

            resume_job_response = await client.post(
                "/api/scheduler/jobs/api-job/resume"
            )
            assert resume_job_response.status_code == 200
            assert resume_job_response.json()["paused"] is False

            missing_job_response = await client.post(
                "/api/scheduler/jobs/missing-job/pause"
            )
            assert missing_job_response.status_code == 404
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_scheduler_api_returns_conflict_when_disabled(tmp_path: Path) -> None:
    scheduler = SchedulerService(
        enabled=False,
        jobstore_url=f"sqlite:///{tmp_path / 'scheduler-api-disabled.sqlite'}",
    )

    app = FastAPI()
    app.include_router(router)
    app.state.scheduler_service = scheduler

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pause_response = await client.post("/api/scheduler/pause")
        assert pause_response.status_code == 409


@pytest.mark.asyncio
async def test_scheduler_api_exposes_job_monitoring_metrics(tmp_path: Path) -> None:
    scheduler = SchedulerService(
        enabled=True,
        jobstore_url=f"sqlite:///{tmp_path / 'scheduler-api-monitoring.sqlite'}",
    )
    processing_pipeline_service = SchedulerProcessingPipelineService(
        scheduler=scheduler,
        settings=get_settings(),
    )
    processing_pipeline_service.register_jobs()

    app = FastAPI()
    app.include_router(router)
    app.state.scheduler_service = scheduler
    app.state.processing_pipeline_service = processing_pipeline_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monitoring_response = await client.get("/api/scheduler/jobs/monitoring")

    assert monitoring_response.status_code == 200
    payload = monitoring_response.json()
    assert {item["job_id"] for item in payload} == {
        URL_SUBMISSION_JOB_ID,
        INDEX_VERIFICATION_JOB_ID,
        SITEMAP_REFRESH_JOB_ID,
    }
