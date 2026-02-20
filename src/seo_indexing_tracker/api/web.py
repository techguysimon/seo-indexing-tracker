"""Web UI routes for server-rendered templates."""

from __future__ import annotations

from collections.abc import Iterable
from math import ceil
from typing import TypedDict
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.models import Sitemap, SitemapType, URL, Website
from seo_indexing_tracker.services.config_validation import (
    ConfigurationValidationError,
    ConfigurationValidationService,
)

router = APIRouter(tags=["web"])

_config_validation_service = ConfigurationValidationService()
_DEFAULT_PAGE_SIZE = 12
_MAX_PAGE_SIZE = 100


class QueueFilters(TypedDict):
    page: int
    page_size: int
    website_id: UUID | None
    queued_only: bool
    search: str


def _form_bool(value: object, *, default: bool) -> bool:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "on", "yes"}:
            return True
        if normalized in {"false", "0", "off", "no", ""}:
            return False
    if isinstance(value, bool):
        return value
    return default


def _form_int(value: object, *, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _form_float(value: object, *, default: float) -> float:
    if isinstance(value, (float, int)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _form_uuid(value: object) -> UUID | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _get_templates(request: Request) -> Jinja2Templates:
    templates = request.app.state.templates
    if isinstance(templates, Jinja2Templates):
        return templates
    raise RuntimeError("Template engine is not configured on application state.")


def _safe_page_size(value: int) -> int:
    return min(max(value, 1), _MAX_PAGE_SIZE)


def _query_filters(
    *,
    page: int,
    page_size: int,
    website_id: UUID | None,
    queued_only: bool,
    search: str,
) -> QueueFilters:
    return {
        "page": max(page, 1),
        "page_size": _safe_page_size(page_size),
        "website_id": website_id,
        "queued_only": queued_only,
        "search": search.strip(),
    }


async def _fetch_dashboard_metrics(session: AsyncSession) -> dict[str, object]:
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
    active_websites = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(Website)
                .where(Website.is_active.is_(True))
            )
        )
        or 0
    )
    tracked_urls = int(
        (await session.scalar(select(func.count()).select_from(URL))) or 0
    )

    queue_by_website_result = await session.execute(
        select(
            Website.domain,
            func.count(URL.id).label("queued_count"),
            func.avg(URL.current_priority).label("average_priority"),
        )
        .join(URL, URL.website_id == Website.id)
        .where(URL.current_priority > 0)
        .group_by(Website.domain)
        .order_by(func.count(URL.id).desc(), Website.domain.asc())
        .limit(6)
    )
    queue_by_website = [
        {
            "domain": row.domain,
            "queued_count": int(row.queued_count or 0),
            "average_priority": float(row.average_priority or 0.0),
        }
        for row in queue_by_website_result
    ]

    return {
        "queued_urls": queued_urls,
        "manual_overrides": manual_overrides,
        "active_websites": active_websites,
        "tracked_urls": tracked_urls,
        "queue_by_website": queue_by_website,
    }


def _base_queue_statement(*, filters: QueueFilters) -> Select[tuple[URL, str]]:
    statement = select(URL, Website.domain).join(Website, Website.id == URL.website_id)
    website_id = filters["website_id"]
    if website_id is not None:
        statement = statement.where(URL.website_id == website_id)

    if filters["queued_only"]:
        statement = statement.where(URL.current_priority > 0)

    search = filters["search"]
    if search:
        statement = statement.where(URL.url.ilike(f"%{search}%"))

    return statement


async def _fetch_queue_rows(
    *,
    session: AsyncSession,
    filters: QueueFilters,
) -> dict[str, object]:
    page = filters["page"]
    page_size = filters["page_size"]
    base_statement = _base_queue_statement(filters=filters)

    count_statement = select(func.count()).select_from(base_statement.subquery())
    total_items = int((await session.scalar(count_statement)) or 0)
    total_pages = max(1, ceil(total_items / page_size))
    safe_page = min(page, total_pages)

    rows = await session.execute(
        base_statement.order_by(URL.current_priority.desc(), URL.updated_at.desc())
        .offset((safe_page - 1) * page_size)
        .limit(page_size)
    )

    items = [
        {
            "id": row[0].id,
            "website_id": row[0].website_id,
            "website_domain": row[1],
            "url": row[0].url,
            "current_priority": row[0].current_priority,
            "manual_priority_override": row[0].manual_priority_override,
            "updated_at": row[0].updated_at,
        }
        for row in rows.all()
    ]

    return {
        "items": items,
        "page": safe_page,
        "page_size": page_size,
        "total_items": total_items,
        "total_pages": total_pages,
    }


async def _fetch_websites(session: AsyncSession) -> list[Website]:
    result = await session.scalars(select(Website).order_by(Website.domain.asc()))
    return list(result)


async def _fetch_sitemaps(
    *,
    session: AsyncSession,
    selected_website_id: UUID | None,
) -> list[Sitemap]:
    if selected_website_id is None:
        return []

    result = await session.scalars(
        select(Sitemap)
        .where(Sitemap.website_id == selected_website_id)
        .order_by(Sitemap.created_at.desc())
    )
    return list(result)


def _table_context(
    *,
    filters: QueueFilters,
    queue_data: dict[str, object],
    websites: Iterable[Website],
    feedback: str | None,
) -> dict[str, object]:
    return {
        "filters": filters,
        "queue_data": queue_data,
        "websites": list(websites),
        "feedback": feedback,
    }


@router.get("/", response_class=Response)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    templates = _get_templates(request)
    metrics = await _fetch_dashboard_metrics(session)
    return templates.TemplateResponse(
        request=request,
        name="pages/dashboard.html",
        context={"page_title": "Dashboard", "metrics": metrics},
    )


@router.get("/web/partials/dashboard-stats", response_class=HTMLResponse)
async def dashboard_stats_partial(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    templates = _get_templates(request)
    metrics = await _fetch_dashboard_metrics(session)
    return templates.TemplateResponse(
        request=request,
        name="partials/dashboard_stats.html",
        context={"metrics": metrics},
    )


@router.get("/web/partials/queue-status", response_class=HTMLResponse)
async def queue_status_partial(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    templates = _get_templates(request)
    queued_count = int(
        (
            await session.scalar(
                select(func.count()).select_from(URL).where(URL.current_priority > 0)
            )
        )
        or 0
    )
    high_priority_count = int(
        (
            await session.scalar(
                select(func.count()).select_from(URL).where(URL.current_priority >= 0.8)
            )
        )
        or 0
    )
    return templates.TemplateResponse(
        request=request,
        name="partials/queue_status.html",
        context={
            "queued_count": queued_count,
            "high_priority_count": high_priority_count,
        },
    )


@router.get("/ui/queue", response_class=Response)
async def queue_management(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    website_id: UUID | None = Query(default=None),
    queued_only: bool = Query(default=True),
    search: str = Query(default=""),
    feedback: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    templates = _get_templates(request)
    websites = await _fetch_websites(session)
    filters = _query_filters(
        page=page,
        page_size=page_size,
        website_id=website_id,
        queued_only=queued_only,
        search=search,
    )
    queue_data = await _fetch_queue_rows(session=session, filters=filters)
    return templates.TemplateResponse(
        request=request,
        name="pages/queue.html",
        context={
            "page_title": "Queue Management",
            **_table_context(
                filters=filters,
                queue_data=queue_data,
                websites=websites,
                feedback=feedback,
            ),
        },
    )


@router.get("/web/partials/queue-table", response_class=HTMLResponse)
async def queue_table_partial(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    website_id: UUID | None = Query(default=None),
    queued_only: bool = Query(default=True),
    search: str = Query(default=""),
    feedback: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    templates = _get_templates(request)
    websites = await _fetch_websites(session)
    filters = _query_filters(
        page=page,
        page_size=page_size,
        website_id=website_id,
        queued_only=queued_only,
        search=search,
    )
    queue_data = await _fetch_queue_rows(session=session, filters=filters)
    return templates.TemplateResponse(
        request=request,
        name="partials/queue_table.html",
        context=_table_context(
            filters=filters,
            queue_data=queue_data,
            websites=websites,
            feedback=feedback,
        ),
    )


@router.post("/web/queue/priority/{url_id}", response_class=HTMLResponse)
async def queue_priority_action(
    url_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    form_data = await request.form()
    priority = _form_float(form_data.get("priority"), default=0.5)
    page = _form_int(form_data.get("page"), default=1)
    page_size = _form_int(form_data.get("page_size"), default=_DEFAULT_PAGE_SIZE)
    website_id = _form_uuid(form_data.get("website_id"))
    queued_only = _form_bool(form_data.get("queued_only"), default=True)
    search = str(form_data.get("search") or "")

    url = await session.get(URL, url_id)
    feedback = "Priority updated"
    if url is None:
        feedback = "URL no longer exists"
    else:
        bounded_priority = max(0.0, min(1.0, priority))
        url.manual_priority_override = bounded_priority
        url.current_priority = bounded_priority
        await session.flush()

    filters = _query_filters(
        page=page,
        page_size=page_size,
        website_id=website_id,
        queued_only=queued_only,
        search=search,
    )
    websites = await _fetch_websites(session)
    queue_data = await _fetch_queue_rows(session=session, filters=filters)
    templates = _get_templates(request)
    return templates.TemplateResponse(
        request=request,
        name="partials/queue_table.html",
        context=_table_context(
            filters=filters,
            queue_data=queue_data,
            websites=websites,
            feedback=feedback,
        ),
    )


@router.post("/web/queue/batch", response_class=HTMLResponse)
async def queue_batch_action(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    form_data = await request.form()
    action = str(form_data.get("action") or "")
    page = _form_int(form_data.get("page"), default=1)
    page_size = _form_int(form_data.get("page_size"), default=_DEFAULT_PAGE_SIZE)
    website_id = _form_uuid(form_data.get("website_id"))
    queued_only = _form_bool(form_data.get("queued_only"), default=True)
    search = str(form_data.get("search") or "")
    url_ids = [
        parsed_id
        for value in form_data.getlist("url_ids")
        if (parsed_id := _form_uuid(value)) is not None
    ]

    feedback = "No URLs selected"
    if url_ids:
        rows = await session.scalars(select(URL).where(URL.id.in_(url_ids)))
        selected_urls = list(rows)

        if action == "enqueue":
            for selected_url in selected_urls:
                if selected_url.manual_priority_override is not None:
                    selected_url.current_priority = (
                        selected_url.manual_priority_override
                    )
                elif selected_url.sitemap_priority is not None:
                    selected_url.current_priority = selected_url.sitemap_priority
                else:
                    selected_url.current_priority = 0.5
            feedback = f"Enqueued {len(selected_urls)} URLs"
        elif action == "remove":
            for selected_url in selected_urls:
                selected_url.current_priority = 0.0
                selected_url.manual_priority_override = None
            feedback = f"Removed {len(selected_urls)} URLs"
        else:
            for selected_url in selected_urls:
                selected_url.manual_priority_override = None
                selected_url.current_priority = max(
                    0.0,
                    min(1.0, float(selected_url.sitemap_priority or 0.5)),
                )
            feedback = f"Recalculated {len(selected_urls)} URLs"

        await session.flush()

    filters = _query_filters(
        page=page,
        page_size=page_size,
        website_id=website_id,
        queued_only=queued_only,
        search=search,
    )
    websites = await _fetch_websites(session)
    queue_data = await _fetch_queue_rows(session=session, filters=filters)
    templates = _get_templates(request)
    return templates.TemplateResponse(
        request=request,
        name="partials/queue_table.html",
        context=_table_context(
            filters=filters,
            queue_data=queue_data,
            websites=websites,
            feedback=feedback,
        ),
    )


@router.get("/ui/websites", response_class=Response)
async def websites_management(
    request: Request,
    feedback: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    templates = _get_templates(request)
    websites = await _fetch_websites(session)
    return templates.TemplateResponse(
        request=request,
        name="pages/websites.html",
        context={
            "page_title": "Website Management",
            "websites": websites,
            "feedback": feedback,
        },
    )


@router.post(
    "/ui/websites", response_class=Response, status_code=status.HTTP_201_CREATED
)
async def create_website_from_web(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    templates = _get_templates(request)
    form_data = await request.form()
    domain = str(form_data.get("domain") or "")
    site_url = str(form_data.get("site_url") or "")
    feedback = "Website created"
    try:
        await _config_validation_service.validate_website_url(site_url=site_url)
        website = Website(
            domain=domain.strip(), site_url=site_url.strip(), is_active=True
        )
        session.add(website)
        await session.flush()
    except ConfigurationValidationError as error:
        feedback = str(error)
    except IntegrityError:
        feedback = "Website with this domain already exists"

    websites = await _fetch_websites(session)
    return templates.TemplateResponse(
        request=request,
        name="pages/websites.html",
        context={
            "page_title": "Website Management",
            "websites": websites,
            "feedback": feedback,
        },
    )


@router.get("/ui/sitemaps", response_class=Response)
async def sitemaps_management(
    request: Request,
    website_id: UUID | None = Query(default=None),
    feedback: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    templates = _get_templates(request)
    websites = await _fetch_websites(session)
    selected_website_id = website_id
    if selected_website_id is None and websites:
        selected_website_id = websites[0].id

    sitemaps = await _fetch_sitemaps(
        session=session,
        selected_website_id=selected_website_id,
    )
    return templates.TemplateResponse(
        request=request,
        name="pages/sitemaps.html",
        context={
            "page_title": "Sitemap Management",
            "websites": websites,
            "selected_website_id": selected_website_id,
            "sitemaps": sitemaps,
            "feedback": feedback,
            "sitemap_types": [SitemapType.URLSET.value, SitemapType.INDEX.value],
        },
    )


@router.post(
    "/ui/sitemaps", response_class=Response, status_code=status.HTTP_201_CREATED
)
async def create_sitemap_from_web(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    templates = _get_templates(request)
    form_data = await request.form()
    website_id = _form_uuid(form_data.get("website_id"))
    url = str(form_data.get("url") or "")
    sitemap_type = str(form_data.get("sitemap_type") or "")
    feedback = "Sitemap created"
    websites = await _fetch_websites(session)

    if website_id is None:
        sitemaps: list[Sitemap] = []
        feedback = "Select a valid website"
        return templates.TemplateResponse(
            request=request,
            name="pages/sitemaps.html",
            context={
                "page_title": "Sitemap Management",
                "websites": websites,
                "selected_website_id": None,
                "sitemaps": sitemaps,
                "feedback": feedback,
                "sitemap_types": [SitemapType.URLSET.value, SitemapType.INDEX.value],
            },
        )

    try:
        await _config_validation_service.validate_sitemap_url(sitemap_url=url)
        parsed_sitemap_type = SitemapType(sitemap_type.strip().upper())
        sitemap = Sitemap(
            website_id=website_id,
            url=url.strip(),
            sitemap_type=parsed_sitemap_type,
            is_active=True,
        )
        session.add(sitemap)
        await session.flush()
    except ConfigurationValidationError as error:
        feedback = str(error)
    except ValueError:
        feedback = "Invalid sitemap type"
    except IntegrityError:
        feedback = "Sitemap already exists for this website"

    sitemaps = await _fetch_sitemaps(session=session, selected_website_id=website_id)
    return templates.TemplateResponse(
        request=request,
        name="pages/sitemaps.html",
        context={
            "page_title": "Sitemap Management",
            "websites": websites,
            "selected_website_id": website_id,
            "sitemaps": sitemaps,
            "feedback": feedback,
            "sitemap_types": [SitemapType.URLSET.value, SitemapType.INDEX.value],
        },
    )
