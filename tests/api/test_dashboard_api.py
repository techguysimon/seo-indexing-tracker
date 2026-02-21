"""E2E-style tests for dashboard observability widgets."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
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
        assert "Index coverage" in stats_response.text
        assert "1 / 2" in stats_response.text
        assert "50.0% indexed" in stats_response.text

        activity_response = await client.get("/web/partials/activity-feed")
        assert activity_response.status_code == 200
        assert "Recent activity" in activity_response.text
        assert "Verification completed for indexed URL" in activity_response.text

        system_status_response = await client.get("/web/partials/system-status")
        assert system_status_response.status_code == 200
        assert "Running jobs" in system_status_response.text
        assert "URL Submission" in system_status_response.text
        assert "Processed 42 URLs" in system_status_response.text

    await engine.dispose()
