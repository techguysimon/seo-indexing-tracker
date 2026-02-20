"""Tests for scheduler processing pipeline jobs and overlap protection."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.sqlite")
os.environ.setdefault("SECRET_KEY", "test-secret")

from seo_indexing_tracker.config import get_settings
from seo_indexing_tracker.services.processing_pipeline import (
    INDEX_VERIFICATION_JOB_ID,
    SITEMAP_REFRESH_JOB_ID,
    URL_SUBMISSION_JOB_ID,
    SchedulerProcessingPipelineService,
    set_scheduler_processing_pipeline_service,
)
from seo_indexing_tracker.services.scheduler import SchedulerService


@pytest.mark.asyncio
async def test_processing_pipeline_registers_scheduler_jobs(tmp_path: Path) -> None:
    scheduler = SchedulerService(
        enabled=True,
        jobstore_url=f"sqlite:///{tmp_path / 'pipeline-jobs.sqlite'}",
    )
    pipeline_service = SchedulerProcessingPipelineService(
        scheduler=scheduler,
        settings=get_settings(),
    )
    set_scheduler_processing_pipeline_service(pipeline_service)

    pipeline_service.register_jobs()
    await scheduler.start()
    try:
        jobs = scheduler.list_jobs()
        assert {job.job_id for job in jobs} == {
            URL_SUBMISSION_JOB_ID,
            INDEX_VERIFICATION_JOB_ID,
            SITEMAP_REFRESH_JOB_ID,
        }
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_processing_pipeline_skips_overlapping_job_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = SchedulerService(
        enabled=True,
        jobstore_url=f"sqlite:///{tmp_path / 'pipeline-overlap.sqlite'}",
    )
    pipeline_service = SchedulerProcessingPipelineService(
        scheduler=scheduler,
        settings=get_settings(),
    )
    set_scheduler_processing_pipeline_service(pipeline_service)
    pipeline_service.register_jobs()
    await scheduler.start()

    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_submit_urls() -> dict[str, int]:
        started.set()
        await release.wait()
        return {"processed_websites": 0, "dequeued_urls": 0, "failed_urls": 0}

    monkeypatch.setattr(pipeline_service, "_submit_urls", fake_submit_urls)

    try:
        first_run = asyncio.create_task(pipeline_service.run_url_submission_job())
        await started.wait()

        await pipeline_service.run_url_submission_job()
        release.set()
        await first_run
    finally:
        await scheduler.shutdown()

    metrics_by_job = {
        metrics.job_id: metrics for metrics in pipeline_service.monitoring_snapshot()
    }
    submission_metrics = metrics_by_job[URL_SUBMISSION_JOB_ID]

    assert submission_metrics.total_runs == 1
    assert submission_metrics.successful_runs == 1
    assert submission_metrics.failed_runs == 0
    assert submission_metrics.overlap_skips == 1
