"""Tests for scheduler service lifecycle and trigger support."""

from __future__ import annotations

from pathlib import Path

import pytest

from seo_indexing_tracker.services.scheduler import SchedulerService


async def _noop_job() -> None:
    return None


@pytest.mark.asyncio
async def test_scheduler_service_supports_interval_and_cron_jobs(
    tmp_path: Path,
) -> None:
    scheduler = SchedulerService(
        enabled=True,
        jobstore_url=f"sqlite:///{tmp_path / 'scheduler-jobs.sqlite'}",
    )

    scheduler.add_interval_job(job_id="interval-job", func=_noop_job, seconds=60)
    scheduler.add_cron_job(
        job_id="cron-job",
        func=_noop_job,
        minute="*/5",
    )

    await scheduler.start()
    try:
        jobs = scheduler.list_jobs()
        assert {job.job_id for job in jobs} == {"interval-job", "cron-job"}
        assert any("interval" in job.trigger.lower() for job in jobs)
        assert any("cron" in job.trigger.lower() for job in jobs)

        paused_job = scheduler.pause_job("interval-job")
        assert paused_job.paused is True

        resumed_job = scheduler.resume_job("interval-job")
        assert resumed_job.paused is False
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_scheduler_service_rejects_operations_when_disabled(
    tmp_path: Path,
) -> None:
    scheduler = SchedulerService(
        enabled=False,
        jobstore_url=f"sqlite:///{tmp_path / 'scheduler-disabled.sqlite'}",
    )

    await scheduler.start()

    assert scheduler.running is False
    with pytest.raises(RuntimeError, match="disabled"):
        scheduler.pause()
