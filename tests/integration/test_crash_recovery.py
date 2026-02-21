"""Chaos-style crash recovery tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from seo_indexing_tracker.models import Base, JobExecution, URL, URLIndexStatus, Website
from seo_indexing_tracker.services.job_recovery_service import JobRecoveryService


@pytest.mark.asyncio
async def test_crash_recovery_marks_running_jobs_failed_and_logs_startup_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'crash-recovery.sqlite'}"
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

    @asynccontextmanager
    async def scoped_session() -> AsyncIterator[AsyncSession]:
        session = session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with scoped_session() as session:
        website = Website(
            domain="recovery.example", site_url="https://recovery.example"
        )
        session.add(website)
        await session.flush()

        session.add(
            URL(
                website_id=website.id,
                url="https://recovery.example/page",
                latest_index_status=URLIndexStatus.UNCHECKED,
            )
        )
        session.add(
            JobExecution(
                job_id="index-verification-job",
                job_name="Index Verification",
                website_id=website.id,
                started_at=datetime.now(UTC),
                status="running",
                urls_processed=18,
                checkpoint_data={"batch": 2},
            )
        )

    recovery_service = JobRecoveryService(session_factory=scoped_session)
    result = await recovery_service.handle_startup_recovery(auto_resume=False)

    assert result.detected_count == 1
    assert result.detected_jobs[0].job_id == "index-verification-job"

    async with scoped_session() as session:
        executions = (await session.execute(select(JobExecution))).scalars().all()

    assert len(executions) == 1
    assert executions[0].status == "failed"
    assert executions[0].error_message == (
        "Recovered unfinished job after process interruption"
    )
    assert executions[0].finished_at is not None
    assert executions[0].checkpoint_data is not None
    assert executions[0].checkpoint_data["stage"] == "startup_recovery"

    from seo_indexing_tracker import main

    monkeypatch.setattr(main, "session_scope", scoped_session)
    caplog.set_level("INFO", logger="seo_indexing_tracker.lifecycle")

    await main._log_startup_recovery_summary(
        interrupted_jobs_detected=1,
        auto_resumed_jobs=0,
    )

    startup_records = [
        record
        for record in caplog.records
        if record.name == "seo_indexing_tracker.lifecycle"
        and record.msg == "startup_recovery_summary"
    ]
    assert len(startup_records) == 1
    summary = startup_records[0]
    assert getattr(summary, "interrupted_jobs_detected", None) == 1
    assert getattr(summary, "auto_resumed_jobs", None) == 0
    url_status_counts = getattr(summary, "url_status_counts", {})
    assert isinstance(url_status_counts, dict)
    assert "UNCHECKED" in url_status_counts

    await engine.dispose()
