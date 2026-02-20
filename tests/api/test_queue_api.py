"""Tests for manual priority queue API operations."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
import os
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.sqlite")
os.environ.setdefault("SECRET_KEY", "test-secret")

from seo_indexing_tracker.api.queue import (
    _get_priority_queue_service,
    _get_queue_admin_token,
    router,
)
from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.models import Base, URL, Website
from seo_indexing_tracker.services.priority_queue import PriorityQueueService

SessionScopeFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass
class QueueApiTestContext:
    app: FastAPI
    engine: AsyncEngine
    session_scope: SessionScopeFactory
    website_id: UUID
    first_url_id: UUID
    second_url_id: UUID


async def _build_context(tmp_path: Path) -> QueueApiTestContext:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'queue-api.sqlite'}"
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
        website = Website(domain="example.com", site_url="https://example.com")
        session.add(website)
        await session.flush()

        first_url = URL(website_id=website.id, url="https://example.com/one")
        second_url = URL(website_id=website.id, url="https://example.com/two")
        session.add_all([first_url, second_url])
        await session.flush()

        website_id = website.id
        first_url_id = first_url.id
        second_url_id = second_url.id

    app = FastAPI()
    app.include_router(router)

    async def override_get_db_session() -> AsyncIterator[AsyncSession]:
        async with scoped_session() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_get_db_session
    app.dependency_overrides[_get_queue_admin_token] = lambda: "test-admin-token"
    app.dependency_overrides[_get_priority_queue_service] = (
        lambda: PriorityQueueService(session_factory=scoped_session)
    )

    return QueueApiTestContext(
        app=app,
        engine=engine,
        session_scope=scoped_session,
        website_id=website_id,
        first_url_id=first_url_id,
        second_url_id=second_url_id,
    )


@pytest.mark.asyncio
async def test_queue_endpoints_require_authorization(tmp_path: Path) -> None:
    context = await _build_context(tmp_path)

    try:
        transport = ASGITransport(app=context.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            endpoint = (
                f"/api/queue/websites/{context.website_id}/urls/{context.first_url_id}"
            )

            missing_auth_response = await client.post(endpoint)
            assert missing_auth_response.status_code == 401

            invalid_auth_response = await client.post(
                endpoint,
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert invalid_auth_response.status_code == 403
    finally:
        await context.engine.dispose()


@pytest.mark.asyncio
async def test_queue_endpoints_add_remove_update_and_clear(tmp_path: Path) -> None:
    context = await _build_context(tmp_path)

    try:
        transport = ASGITransport(app=context.app)
        auth_headers = {"Authorization": "Bearer test-admin-token"}

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            add_response = await client.post(
                f"/api/queue/websites/{context.website_id}/urls/{context.first_url_id}",
                headers=auth_headers,
            )
            assert add_response.status_code == 200
            assert add_response.json()["current_priority"] > 0

            update_response = await client.patch(
                "/api/queue/websites/"
                f"{context.website_id}/urls/{context.first_url_id}/priority",
                headers=auth_headers,
                json={"priority": 0.95},
            )
            assert update_response.status_code == 200
            assert update_response.json()["current_priority"] == 0.95
            assert update_response.json()["manual_priority_override"] == 0.95

            remove_response = await client.delete(
                f"/api/queue/websites/{context.website_id}/urls/{context.first_url_id}",
                headers=auth_headers,
            )
            assert remove_response.status_code == 200
            assert remove_response.json()["current_priority"] == 0

            second_add_response = await client.post(
                f"/api/queue/websites/{context.website_id}/urls/{context.first_url_id}",
                headers=auth_headers,
            )
            assert second_add_response.status_code == 200

            add_second_url_response = await client.post(
                f"/api/queue/websites/{context.website_id}/urls/{context.second_url_id}",
                headers=auth_headers,
            )
            assert add_second_url_response.status_code == 200

            clear_response = await client.delete(
                f"/api/queue/websites/{context.website_id}",
                headers=auth_headers,
            )
            assert clear_response.status_code == 200
            assert clear_response.json()["website_id"] == str(context.website_id)
            assert clear_response.json()["cleared_count"] == 2

        async with context.session_scope() as session:
            first_url = await session.get(URL, context.first_url_id)
            second_url = await session.get(URL, context.second_url_id)

            assert first_url is not None
            assert second_url is not None
            assert first_url.current_priority == 0
            assert first_url.manual_priority_override is None
            assert second_url.current_priority == 0
            assert second_url.manual_priority_override is None
    finally:
        await context.engine.dispose()
