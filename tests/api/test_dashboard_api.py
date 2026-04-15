"""E2E-style tests for dashboard observability widgets."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.sqlite")
os.environ.setdefault("SECRET_KEY", "test-secret")

from seo_indexing_tracker.api.web import router
from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.models import (
    ActivityLog,
    Base,
    JobExecution,
    URL,
    URLIndexStatus,
    Website,
)


@pytest.mark.asyncio
async def test_dashboard_partials_include_observability_data(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'dashboard-api.sqlite'}"
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        website = Website(domain="widgets.example", site_url="https://widgets.example")
        session.add(website)
        await session.flush()

        session.add_all(
            [
                URL(
                    website_id=website.id,
                    url="https://widgets.example/indexed",
                    current_priority=0.9,
                    latest_index_status=URLIndexStatus.INDEXED,
                ),
                URL(
                    website_id=website.id,
                    url="https://widgets.example/not-indexed",
                    current_priority=0.4,
                    latest_index_status=URLIndexStatus.NOT_INDEXED,
                ),
            ]
        )
        session.add(
            ActivityLog(
                event_type="url_verified",
                website_id=website.id,
                message="Verification completed for indexed URL",
                metadata_json={"url": "https://widgets.example/indexed"},
            )
        )
        session.add(
            JobExecution(
                job_id="url-submission-job",
                job_name="URL Submission",
                website_id=website.id,
                started_at=datetime.now(UTC),
                status="running",
                urls_processed=42,
                checkpoint_data={"batch": 3},
            )
        )
        await session.commit()

    app = FastAPI()
    app.include_router(router)
    app.state.templates = Jinja2Templates(
        directory=str(
            Path(__file__).resolve().parents[2]
            / "src"
            / "seo_indexing_tracker"
            / "templates"
        )
    )

    from zoneinfo import ZoneInfo

    def _datetime_us(value: datetime | None) -> str:
        if value is None:
            return "Never"
        eastern_tz = ZoneInfo("America/New_York")
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        value_eastern = value.astimezone(eastern_tz)
        return value_eastern.strftime("%-m-%-d-%Y %-I:%M %p")

    def _datetime_relative(value: datetime | None) -> str:
        if value is None:
            return "Never"

        now = datetime.now(UTC)
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        delta = now - value
        if delta < timedelta(minutes=1):
            return "just now"
        if delta < timedelta(hours=1):
            minutes = int(delta.total_seconds() / 60)
            return f"{minutes} min{'s' if minutes != 1 else ''} ago"
        if delta < timedelta(days=1):
            hours = int(delta.total_seconds() / 3600)
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        return value.strftime("%-m-%-d-%Y")

    app.state.templates.env.filters["datetime_us"] = _datetime_us
    app.state.templates.env.filters["datetime_relative"] = _datetime_relative
    app.state.templates.env.filters["humanize_date"] = _datetime_relative

    async def override_get_db_session() -> AsyncIterator[AsyncSession]:
        session = session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    app.dependency_overrides[get_db_session] = override_get_db_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        stats_response = await client.get("/web/partials/dashboard-stats")
        assert stats_response.status_code == 200
        assert "Submitted Today" in stats_response.text
        assert "Verified Today" in stats_response.text
        assert "Queued URLs" in stats_response.text
        assert "Indexing Quota" in stats_response.text

        activity_response = await client.get("/web/partials/activity-feed")
        assert activity_response.status_code == 200
        assert "Activity Feed" in activity_response.text
        assert "Verification completed for indexed URL" in activity_response.text

        system_status_response = await client.get("/web/partials/system-status")
        assert system_status_response.status_code == 200
        assert "System Status" in system_status_response.text
        assert "Submission" in system_status_response.text
        assert "42" in system_status_response.text  # URLs processed count

        queue_status_response = await client.get("/web/partials/queue-status")
        assert queue_status_response.status_code == 200
        assert "Live Queue Status" in queue_status_response.text
        assert "Queued Now" in queue_status_response.text

        queue_dist_response = await client.get("/web/partials/queue-distribution")
        assert queue_dist_response.status_code == 200
        assert "Low" in queue_dist_response.text
        assert "High" in queue_dist_response.text

        index_coverage_response = await client.get("/web/partials/index-coverage")
        assert index_coverage_response.status_code == 200
        assert "Indexed" in index_coverage_response.text
        assert "50.0%" in index_coverage_response.text

        website_coverage_response = await client.get("/web/partials/website-coverage")
        assert website_coverage_response.status_code == 200
        assert "widgets.example" in website_coverage_response.text

    await engine.dispose()
