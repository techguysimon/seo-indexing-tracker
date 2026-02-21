"""Web UI routes for server-rendered templates."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
import logging
from math import ceil
from typing import TypedDict
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.models import (
    ServiceAccount,
    Sitemap,
    SitemapType,
    URL,
    Website,
)
from seo_indexing_tracker.services.config_validation import (
    ConfigurationValidationError,
    ConfigurationValidationService,
)
from seo_indexing_tracker.services.priority_queue import PriorityQueueService
from seo_indexing_tracker.services.sitemap_fetcher import (
    SitemapFetchError,
    SitemapFetchNetworkError,
    SitemapFetchTimeoutError,
    SitemapFetchHTTPError,
)
from seo_indexing_tracker.services.url_discovery import (
    URLDiscoveryProcessingError,
    URLDiscoveryService,
)

router = APIRouter(tags=["web"])

_config_validation_service = ConfigurationValidationService()
_trigger_logger = logging.getLogger("seo_indexing_tracker.web.trigger_indexing")
_DEFAULT_PAGE_SIZE = 12
_MAX_PAGE_SIZE = 100
_ALLOWED_SERVICE_ACCOUNT_SCOPES = frozenset({"indexing", "webmasters"})


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


def _safe_sitemap_url_for_feedback(url: str | None) -> str:
    if not url:
        return "sitemap"

    split_url = urlsplit(url)
    host = split_url.netloc.rsplit("@", maxsplit=1)[-1]
    path = split_url.path or "/"
    safe_url = f"{host}{path}".strip()
    return safe_url or "sitemap"


def _trigger_feedback_for_fetch_error(
    *,
    error: SitemapFetchError,
    safe_sitemap_url: str,
) -> str:
    if isinstance(error, SitemapFetchTimeoutError):
        return (
            "Trigger indexing failed: network timeout while fetching sitemap "
            f"({safe_sitemap_url}). Retry in a moment."
        )

    if isinstance(error, SitemapFetchNetworkError):
        return (
            "Trigger indexing failed: network error while fetching sitemap "
            f"({safe_sitemap_url}). Verify DNS/firewall access and retry."
        )

    if isinstance(error, SitemapFetchHTTPError):
        if error.status_code in {401, 403}:
            return (
                "Trigger indexing failed: sitemap access blocked "
                f"({safe_sitemap_url}, HTTP {error.status_code}). "
                "Verify sitemap access rules and retry."
            )

        return (
            "Trigger indexing failed: sitemap fetch returned an HTTP error "
            f"({safe_sitemap_url}, HTTP {error.status_code})."
        )

    return (
        "Trigger indexing failed: unable to fetch sitemap "
        f"({safe_sitemap_url}). Verify sitemap access rules and retry."
    )


def _trigger_feedback_for_discovery_error(
    *,
    error: URLDiscoveryProcessingError,
    safe_sitemap_url: str,
) -> str:
    if error.stage == "parse":
        return (
            "Trigger indexing failed: sitemap response was not valid XML "
            f"({safe_sitemap_url})."
        )

    return (
        "Trigger indexing failed: sitemap discovery failed "
        f"({safe_sitemap_url}) before URLs could be queued."
    )


def _is_htmx_request(request: Request) -> bool:
    return request.headers.get("HX-Request", "").strip().lower() == "true"


@asynccontextmanager
async def _use_existing_session(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    yield session


def _normalize_scopes(scopes: list[str]) -> list[str]:
    normalized_scopes = [scope.strip().lower() for scope in scopes]
    invalid_scopes = sorted(
        {
            scope
            for scope in normalized_scopes
            if scope and scope not in _ALLOWED_SERVICE_ACCOUNT_SCOPES
        }
    )
    if invalid_scopes:
        invalid_scopes_text = ", ".join(invalid_scopes)
        raise ConfigurationValidationError(
            f"Invalid scopes: {invalid_scopes_text}. Allowed scopes: indexing, webmasters"
        )

    unique_scopes: list[str] = []
    seen_scopes: set[str] = set()
    for scope in normalized_scopes:
        if not scope or scope in seen_scopes:
            continue
        unique_scopes.append(scope)
        seen_scopes.add(scope)
    return unique_scopes


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


async def _fetch_website_with_details(
    *,
    session: AsyncSession,
    website_id: UUID,
) -> Website | None:
    statement = (
        select(Website)
        .where(Website.id == website_id)
        .options(
            selectinload(Website.service_account),
            selectinload(Website.sitemaps),
        )
    )
    website: Website | None = await session.scalar(statement)
    return website


async def _render_website_detail(
    *,
    request: Request,
    session: AsyncSession,
    website_id: UUID,
    feedback: str | None,
) -> Response:
    templates = _get_templates(request)
    website = await _fetch_website_with_details(session=session, website_id=website_id)
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found",
        )

    context = {
        "page_title": f"{website.domain} Setup",
        "website": website,
        "service_account": website.service_account,
        "sitemaps": sorted(
            website.sitemaps,
            key=lambda sitemap: sitemap.created_at,
            reverse=True,
        ),
        "sitemap_types": [SitemapType.URLSET.value, SitemapType.INDEX.value],
        "feedback": feedback,
    }
    template_name = (
        "partials/website_detail_panel.html"
        if _is_htmx_request(request)
        else "pages/website_detail.html"
    )
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context=context,
    )


async def _rollback_and_render_website_detail(
    *,
    request: Request,
    session: AsyncSession,
    website_id: UUID,
    feedback: str,
) -> Response:
    await session.rollback()
    return await _render_website_detail(
        request=request,
        session=session,
        website_id=website_id,
        feedback=feedback,
    )


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


@router.get("/websites/{website_id}", response_class=Response)
@router.get("/ui/websites/{website_id}", response_class=Response)
async def website_detail(
    request: Request,
    website_id: UUID,
    feedback: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Website detail page with service account and sitemap management."""
    return await _render_website_detail(
        request=request,
        session=session,
        website_id=website_id,
        feedback=feedback,
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


@router.post("/websites/{website_id}/service-account", response_class=Response)
@router.post("/ui/websites/{website_id}/service-account", response_class=Response)
async def create_service_account_from_web(
    request: Request,
    website_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Create service account for a website via web form."""
    form_data = await request.form()
    name = str(form_data.get("name") or "").strip()
    credentials_path = str(form_data.get("credentials_path") or "").strip()
    scopes = [str(scope) for scope in form_data.getlist("scopes")]
    feedback = "Service account created"

    website = await session.get(Website, website_id)
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found",
        )

    existing_service_account_id = await session.scalar(
        select(ServiceAccount.id).where(ServiceAccount.website_id == website_id)
    )
    if existing_service_account_id is not None:
        feedback = "Service account already exists for this website"
        return await _render_website_detail(
            request=request,
            session=session,
            website_id=website_id,
            feedback=feedback,
        )

    try:
        normalized_scopes = _normalize_scopes(scopes)
        validated_credentials_path = (
            await _config_validation_service.validate_service_account(
                credentials_path=credentials_path,
                scopes=normalized_scopes,
            )
        )
        service_account = ServiceAccount(
            website_id=website_id,
            name=name,
            credentials_path=validated_credentials_path,
            scopes=normalized_scopes,
        )
        session.add(service_account)
        await session.flush()
    except ConfigurationValidationError as error:
        feedback = str(error)
    except IntegrityError:
        feedback = "Service account already exists for this website"

    return await _render_website_detail(
        request=request,
        session=session,
        website_id=website_id,
        feedback=feedback,
    )


@router.post("/websites/{website_id}/service-account/delete", response_class=Response)
@router.post(
    "/ui/websites/{website_id}/service-account/delete", response_class=Response
)
async def delete_service_account_from_web(
    request: Request,
    website_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Delete service account for a website via web UI."""
    website = await session.get(Website, website_id)
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found",
        )

    service_account = await session.scalar(
        select(ServiceAccount).where(ServiceAccount.website_id == website_id)
    )
    feedback = "Service account not found"
    if service_account is not None:
        await session.delete(service_account)
        await session.flush()
        feedback = "Service account deleted"

    return await _render_website_detail(
        request=request,
        session=session,
        website_id=website_id,
        feedback=feedback,
    )


@router.post("/websites/{website_id}/sitemaps", response_class=Response)
@router.post("/ui/websites/{website_id}/sitemaps", response_class=Response)
async def create_sitemap_for_website_from_web(
    request: Request,
    website_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Create sitemap for a website from website detail page."""
    form_data = await request.form()
    url = str(form_data.get("url") or "")
    sitemap_type = str(form_data.get("sitemap_type") or "")
    feedback = "Sitemap created"

    website = await session.get(Website, website_id)
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found",
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

    return await _render_website_detail(
        request=request,
        session=session,
        website_id=website_id,
        feedback=feedback,
    )


@router.post("/sitemaps/{sitemap_id}/delete", response_class=Response)
@router.post("/ui/sitemaps/{sitemap_id}/delete", response_class=Response)
async def delete_sitemap_from_web(
    request: Request,
    sitemap_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Delete sitemap via website detail page."""
    sitemap = await session.get(Sitemap, sitemap_id)
    if sitemap is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sitemap not found",
        )

    website_id = sitemap.website_id
    await session.delete(sitemap)
    await session.flush()
    return await _render_website_detail(
        request=request,
        session=session,
        website_id=website_id,
        feedback="Sitemap deleted",
    )


@router.post("/websites/{website_id}/trigger", response_class=Response)
@router.post("/ui/websites/{website_id}/trigger", response_class=Response)
async def trigger_indexing(
    request: Request,
    website_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Trigger URL discovery and queueing for a website."""
    website = await session.get(Website, website_id)
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found",
        )

    discovery_service = URLDiscoveryService(
        session_factory=lambda: _use_existing_session(session)
    )
    queue_service = PriorityQueueService(
        session_factory=lambda: _use_existing_session(session)
    )
    sitemap_rows = (
        await session.execute(
            select(Sitemap.id, Sitemap.url).where(
                Sitemap.website_id == website_id,
                Sitemap.is_active.is_(True),
            )
        )
    ).all()
    sitemap_ids = [row.id for row in sitemap_rows]
    sitemap_urls_by_id = {row.id: row.url for row in sitemap_rows}

    discovered_urls = 0
    for sitemap_id in sitemap_ids:
        sitemap_url = sitemap_urls_by_id.get(sitemap_id)
        safe_sitemap_url = _safe_sitemap_url_for_feedback(sitemap_url)
        try:
            discovery_result = await discovery_service.discover_urls(sitemap_id)
            discovered_urls += (
                discovery_result.new_count + discovery_result.modified_count
            )
        except SitemapFetchError as error:
            error_url = getattr(error, "url", None)
            safe_error_url = (
                _safe_sitemap_url_for_feedback(error_url) if error_url else None
            )
            _trigger_logger.error(
                {
                    "event": "trigger_indexing_failed",
                    "website_id": str(website_id),
                    "sitemap_id": str(sitemap_id),
                    "sitemap_url_sanitized": safe_error_url or safe_sitemap_url,
                    "stage": "fetch",
                    "exception_class": error.__class__.__name__,
                    "http_status": getattr(error, "status_code", None),
                    "content_type": getattr(error, "content_type", None),
                }
            )
            feedback = _trigger_feedback_for_fetch_error(
                error=error,
                safe_sitemap_url=safe_error_url or safe_sitemap_url,
            )
            return await _rollback_and_render_website_detail(
                request=request,
                session=session,
                website_id=website_id,
                feedback=feedback,
            )
        except URLDiscoveryProcessingError as error:
            _trigger_logger.error(
                {
                    "event": "trigger_indexing_failed",
                    "website_id": str(website_id),
                    "sitemap_id": str(sitemap_id),
                    "sitemap_url_sanitized": safe_sitemap_url,
                    "stage": error.stage,
                    "exception_class": error.__class__.__name__,
                    "http_status": error.status_code,
                    "content_type": error.content_type,
                }
            )
            feedback = _trigger_feedback_for_discovery_error(
                error=error,
                safe_sitemap_url=safe_sitemap_url,
            )
            return await _rollback_and_render_website_detail(
                request=request,
                session=session,
                website_id=website_id,
                feedback=feedback,
            )

    website_url_ids = list(
        await session.scalars(select(URL.id).where(URL.website_id == website_id))
    )
    enqueue_context_sitemap_url = _safe_sitemap_url_for_feedback(
        next(iter(sitemap_urls_by_id.values()), None)
    )
    try:
        queued_urls = await queue_service.enqueue_many(website_url_ids)
    except Exception as error:
        _trigger_logger.error(
            {
                "event": "trigger_indexing_failed",
                "website_id": str(website_id),
                "sitemap_id": None,
                "sitemap_url_sanitized": enqueue_context_sitemap_url,
                "stage": "enqueue",
                "exception_class": error.__class__.__name__,
                "http_status": None,
                "content_type": None,
            }
        )
        return await _rollback_and_render_website_detail(
            request=request,
            session=session,
            website_id=website_id,
            feedback=(
                "Trigger indexing failed: discovered URLs could not be queued. "
                "Review server logs for enqueue diagnostics and retry."
            ),
        )
    feedback = (
        "Indexing triggered: "
        f"refreshed {len(sitemap_ids)} sitemaps, "
        f"discovered {discovered_urls} URLs, "
        f"queued {queued_urls} URLs"
    )
    return await _render_website_detail(
        request=request,
        session=session,
        website_id=website_id,
        feedback=feedback,
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
