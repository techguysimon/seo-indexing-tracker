"""Manual priority queue management API routes."""

from __future__ import annotations

import logging
from secrets import compare_digest
from math import ceil
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.config import get_settings
from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.models import URL, Website
from seo_indexing_tracker.schemas import URLRead
from seo_indexing_tracker.services import PriorityQueueService

router = APIRouter(prefix="/api/queue", tags=["queue"])

_bearer_auth = HTTPBearer(auto_error=False)
_audit_logger = logging.getLogger("seo_indexing_tracker.audit.queue")


class QueuePriorityUpdateRequest(BaseModel):
    """Manual priority update payload."""

    priority: float = Field(ge=0.0, le=1.0)


class QueueClearResponse(BaseModel):
    """Clear queue response metadata."""

    website_id: UUID
    cleared_count: int


class QueueItemRead(BaseModel):
    """Serialized queue item with website context."""

    id: UUID
    website_id: UUID
    website_domain: str
    url: str
    current_priority: float
    manual_priority_override: float | None


class QueueListResponse(BaseModel):
    """Paginated queue listing response."""

    page: int
    page_size: int
    total_items: int
    total_pages: int
    items: list[QueueItemRead]


class QueueStatsResponse(BaseModel):
    """Aggregate queue stats for dashboard views."""

    queued_urls: int
    manual_overrides: int
    websites_with_queued_urls: int
    average_priority: float


class QueueRuntimeStatusResponse(BaseModel):
    """Lightweight status for frequent polling."""

    queued_urls: int
    high_priority_urls: int
    websites_ready: int


class QueueBatchRequest(BaseModel):
    """Batch queue operation payload."""

    operation: Literal["enqueue", "remove", "reprioritize"]
    url_ids: list[UUID] = Field(min_length=1, max_length=500)
    priority: float | None = Field(default=None, ge=0.0, le=1.0)


class QueueBatchResponse(BaseModel):
    """Batch operation result metadata."""

    operation: str
    processed_count: int
    failed_count: int
    errors: list[str]


class QueueManualTriggerRequest(BaseModel):
    """Website-scoped manual trigger payload."""

    action: Literal["enqueue_all", "clear_queue", "reset_overrides"]


class QueueManualTriggerResponse(BaseModel):
    """Manual trigger execution metadata."""

    website_id: UUID
    action: str
    affected_count: int


def _get_queue_admin_token() -> str:
    return get_settings().SECRET_KEY.get_secret_value()


def _get_priority_queue_service() -> PriorityQueueService:
    return PriorityQueueService()


async def _require_queue_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_auth),
    expected_token: str = Depends(_get_queue_admin_token),
) -> None:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization credentials",
        )

    if credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization scheme must be Bearer",
        )

    if compare_digest(credentials.credentials, expected_token):
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Not authorized to manage queue",
    )


def _audit_manual_action(
    *,
    request: Request,
    action: str,
    website_id: UUID,
    url_id: UUID | None = None,
    priority: float | None = None,
    cleared_count: int | None = None,
) -> None:
    _audit_logger.info(
        {
            "event": "manual_queue_intervention",
            "action": action,
            "website_id": str(website_id),
            "url_id": str(url_id) if url_id is not None else None,
            "priority": priority,
            "cleared_count": cleared_count,
            "client_ip": request.client.host if request.client is not None else None,
        }
    )


async def _get_url_or_404(
    *,
    website_id: UUID,
    url_id: UUID,
    session: AsyncSession,
) -> URL:
    url = await session.scalar(
        select(URL).where(URL.id == url_id, URL.website_id == website_id)
    )
    if url is not None:
        return url

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="URL not found",
    )


async def _ensure_website_exists(*, website_id: UUID, session: AsyncSession) -> None:
    website = await session.scalar(select(Website.id).where(Website.id == website_id))
    if website is not None:
        return

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Website not found",
    )


@router.post(
    "/websites/{website_id}/urls/{url_id}",
    response_model=URLRead,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(_require_queue_admin)],
)
async def add_url_to_queue(
    website_id: UUID,
    url_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    queue_service: PriorityQueueService = Depends(_get_priority_queue_service),
) -> URL:
    await _get_url_or_404(website_id=website_id, url_id=url_id, session=session)

    try:
        queued_url = await queue_service.enqueue(url_id)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="URL not found",
        ) from error

    _audit_manual_action(
        request=request,
        action="add",
        website_id=website_id,
        url_id=url_id,
        priority=queued_url.current_priority,
    )
    return await _get_url_or_404(website_id=website_id, url_id=url_id, session=session)


@router.delete(
    "/websites/{website_id}/urls/{url_id}",
    response_model=URLRead,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(_require_queue_admin)],
)
async def remove_url_from_queue(
    website_id: UUID,
    url_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    queue_service: PriorityQueueService = Depends(_get_priority_queue_service),
) -> URL:
    await _get_url_or_404(website_id=website_id, url_id=url_id, session=session)

    try:
        removed_url = await queue_service.remove(url_id)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="URL not found",
        ) from error

    _audit_manual_action(
        request=request,
        action="remove",
        website_id=website_id,
        url_id=url_id,
        priority=removed_url.current_priority,
    )
    return await _get_url_or_404(website_id=website_id, url_id=url_id, session=session)


@router.patch(
    "/websites/{website_id}/urls/{url_id}/priority",
    response_model=URLRead,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(_require_queue_admin)],
)
async def update_url_queue_priority(
    website_id: UUID,
    url_id: UUID,
    payload: QueuePriorityUpdateRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    queue_service: PriorityQueueService = Depends(_get_priority_queue_service),
) -> URL:
    await _get_url_or_404(website_id=website_id, url_id=url_id, session=session)

    try:
        reprioritized_url = await queue_service.reprioritize(
            url_id,
            manual_override=payload.priority,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="URL not found",
        ) from error

    _audit_manual_action(
        request=request,
        action="reprioritize",
        website_id=website_id,
        url_id=url_id,
        priority=reprioritized_url.current_priority,
    )
    return await _get_url_or_404(website_id=website_id, url_id=url_id, session=session)


@router.delete(
    "/websites/{website_id}",
    response_model=QueueClearResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(_require_queue_admin)],
)
async def clear_website_queue(
    website_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> QueueClearResponse:
    await _ensure_website_exists(website_id=website_id, session=session)

    queued_count = await session.scalar(
        select(func.count())
        .select_from(URL)
        .where(URL.website_id == website_id, URL.current_priority > 0)
    )
    await session.execute(
        update(URL)
        .where(URL.website_id == website_id)
        .values(current_priority=0.0, manual_priority_override=None)
    )

    cleared_count = int(queued_count or 0)
    _audit_manual_action(
        request=request,
        action="clear",
        website_id=website_id,
        cleared_count=cleared_count,
    )
    return QueueClearResponse(website_id=website_id, cleared_count=cleared_count)


@router.get("/stats", response_model=QueueStatsResponse, status_code=status.HTTP_200_OK)
async def queue_stats(
    session: AsyncSession = Depends(get_db_session),
) -> QueueStatsResponse:
    queued_urls = int(
        (
            await session.scalar(
                select(func.count()).select_from(URL).where(URL.current_priority > 0)
            )
        )
        or 0
    )
    manual_overrides = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(URL)
                .where(URL.manual_priority_override.is_not(None))
            )
        )
        or 0
    )
    websites_with_queued_urls = int(
        (
            await session.scalar(
                select(func.count(func.distinct(URL.website_id))).where(
                    URL.current_priority > 0
                )
            )
        )
        or 0
    )
    average_priority = float(
        (
            await session.scalar(
                select(func.avg(URL.current_priority)).where(URL.current_priority > 0)
            )
        )
        or 0.0
    )

    return QueueStatsResponse(
        queued_urls=queued_urls,
        manual_overrides=manual_overrides,
        websites_with_queued_urls=websites_with_queued_urls,
        average_priority=round(average_priority, 4),
    )


@router.get(
    "/status", response_model=QueueRuntimeStatusResponse, status_code=status.HTTP_200_OK
)
async def queue_runtime_status(
    session: AsyncSession = Depends(get_db_session),
) -> QueueRuntimeStatusResponse:
    queued_urls = int(
        (
            await session.scalar(
                select(func.count()).select_from(URL).where(URL.current_priority > 0)
            )
        )
        or 0
    )
    high_priority_urls = int(
        (
            await session.scalar(
                select(func.count()).select_from(URL).where(URL.current_priority >= 0.8)
            )
        )
        or 0
    )
    websites_ready = int(
        (
            await session.scalar(
                select(func.count(func.distinct(URL.website_id))).where(
                    URL.current_priority >= 0.6
                )
            )
        )
        or 0
    )
    return QueueRuntimeStatusResponse(
        queued_urls=queued_urls,
        high_priority_urls=high_priority_urls,
        websites_ready=websites_ready,
    )


@router.get("/items", response_model=QueueListResponse, status_code=status.HTTP_200_OK)
async def list_queue_items(
    website_id: UUID | None = None,
    queued_only: bool = True,
    has_manual_override: bool | None = None,
    search: str = "",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
) -> QueueListResponse:
    statement = select(URL, Website.domain).join(Website, Website.id == URL.website_id)
    if website_id is not None:
        statement = statement.where(URL.website_id == website_id)
    if queued_only:
        statement = statement.where(URL.current_priority > 0)
    if has_manual_override is True:
        statement = statement.where(URL.manual_priority_override.is_not(None))
    elif has_manual_override is False:
        statement = statement.where(URL.manual_priority_override.is_(None))

    stripped_search = search.strip()
    if stripped_search:
        statement = statement.where(URL.url.ilike(f"%{stripped_search}%"))

    count_query = select(func.count()).select_from(statement.subquery())
    total_items = int((await session.scalar(count_query)) or 0)
    total_pages = max(1, ceil(total_items / page_size))
    safe_page = min(page, total_pages)

    rows = await session.execute(
        statement.order_by(URL.current_priority.desc(), URL.updated_at.desc())
        .offset((safe_page - 1) * page_size)
        .limit(page_size)
    )
    items = [
        QueueItemRead(
            id=row[0].id,
            website_id=row[0].website_id,
            website_domain=row[1],
            url=row[0].url,
            current_priority=row[0].current_priority,
            manual_priority_override=row[0].manual_priority_override,
        )
        for row in rows.all()
    ]

    return QueueListResponse(
        page=safe_page,
        page_size=page_size,
        total_items=total_items,
        total_pages=total_pages,
        items=items,
    )


@router.post(
    "/batch",
    response_model=QueueBatchResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(_require_queue_admin)],
)
async def queue_batch_operation(
    payload: QueueBatchRequest,
    request: Request,
    queue_service: PriorityQueueService = Depends(_get_priority_queue_service),
) -> QueueBatchResponse:
    errors: list[str] = []
    processed_count = 0

    for url_id in payload.url_ids:
        try:
            if payload.operation == "enqueue":
                queued_url = await queue_service.enqueue(url_id)
                _audit_manual_action(
                    request=request,
                    action="batch_enqueue",
                    website_id=queued_url.website_id,
                    url_id=queued_url.id,
                    priority=queued_url.current_priority,
                )
            elif payload.operation == "remove":
                removed_url = await queue_service.remove(url_id)
                _audit_manual_action(
                    request=request,
                    action="batch_remove",
                    website_id=removed_url.website_id,
                    url_id=removed_url.id,
                    priority=removed_url.current_priority,
                )
            else:
                if payload.priority is None:
                    errors.append(f"{url_id}: priority is required for reprioritize")
                    continue
                reprioritized_url = await queue_service.reprioritize(
                    url_id,
                    manual_override=payload.priority,
                )
                _audit_manual_action(
                    request=request,
                    action="batch_reprioritize",
                    website_id=reprioritized_url.website_id,
                    url_id=reprioritized_url.id,
                    priority=reprioritized_url.current_priority,
                )
            processed_count += 1
        except ValueError:
            errors.append(f"{url_id}: URL not found")

    return QueueBatchResponse(
        operation=payload.operation,
        processed_count=processed_count,
        failed_count=len(errors),
        errors=errors,
    )


@router.post(
    "/websites/{website_id}/trigger",
    response_model=QueueManualTriggerResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(_require_queue_admin)],
)
async def queue_manual_trigger(
    website_id: UUID,
    payload: QueueManualTriggerRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    queue_service: PriorityQueueService = Depends(_get_priority_queue_service),
) -> QueueManualTriggerResponse:
    await _ensure_website_exists(website_id=website_id, session=session)

    if payload.action == "clear_queue":
        queued_count = await session.scalar(
            select(func.count())
            .select_from(URL)
            .where(URL.website_id == website_id, URL.current_priority > 0)
        )
        await session.execute(
            update(URL)
            .where(URL.website_id == website_id)
            .values(current_priority=0.0, manual_priority_override=None)
        )
        affected_count = int(queued_count or 0)
    else:
        url_ids = list(
            await session.scalars(select(URL.id).where(URL.website_id == website_id))
        )
        if payload.action == "reset_overrides":
            await session.execute(
                update(URL)
                .where(URL.website_id == website_id)
                .values(manual_priority_override=None)
            )
        affected_count = await queue_service.enqueue_many(url_ids)

    _audit_manual_action(
        request=request,
        action=f"manual_trigger_{payload.action}",
        website_id=website_id,
        cleared_count=affected_count,
    )

    return QueueManualTriggerResponse(
        website_id=website_id,
        action=payload.action,
        affected_count=affected_count,
    )
