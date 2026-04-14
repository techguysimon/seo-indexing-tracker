"""Web UI routes for server-rendered templates."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import logging
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.api.auth import require_admin
from seo_indexing_tracker.schemas.auth import UserInfo
from seo_indexing_tracker.api.urls import (
    WebsiteURLListResponse,
    ensure_website_exists,
    fetch_website_urls,
    list_website_sitemaps,
)
from seo_indexing_tracker.utils.form_helpers import (
    _form_bool,
    _form_float,
    _form_int,
    _form_uuid,
)
from seo_indexing_tracker.models import (
    QuotaUsage,
    ServiceAccount,
    Sitemap,
    SitemapType,
    URL,
    URLIndexStatus,
    Website,
)
from seo_indexing_tracker.models.website import QuotaDiscoveryStatus
from seo_indexing_tracker.services.activity_service import ActivityService
from seo_indexing_tracker.services.url_inspection_service import (
    inspect_single_url as inspect_single_url_service,
)
from seo_indexing_tracker.services.url_submission_service import (
    submit_single_url as submit_single_url_service,
)
from seo_indexing_tracker.services.config_validation import (
    ConfigurationValidationError,
    ConfigurationValidationService,
)

from seo_indexing_tracker.services.quota_discovery_service import QuotaDiscoveryService
from seo_indexing_tracker.services.queue_eta_service import QueueETAService
from seo_indexing_tracker.services.website_detail_service import (
    build_website_detail_context,
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
from seo_indexing_tracker.services.trigger_indexing_service import (
    EnqueueException,
    SitemapFetchException,
    TriggerIndexingService,
    URLDiscoveryProcessingException,
)
from seo_indexing_tracker.services.queue_template_service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    _fetch_queue_rows,
    _query_filters,
    _table_context,
)
from seo_indexing_tracker.services.dashboard_service import (
    _build_system_status_context,
    _fetch_dashboard_metrics,
    _fetch_recent_activity,
)


router = APIRouter(tags=["web"])

_config_validation_service = ConfigurationValidationService()
_quota_discovery_service = QuotaDiscoveryService()
_activity_service = ActivityService()
_trigger_logger = logging.getLogger("seo_indexing_tracker.web.trigger_indexing")
_ALLOWED_SERVICE_ACCOUNT_SCOPES = frozenset({"indexing", "webmasters"})


def _get_templates(request: Request) -> Jinja2Templates:
    templates = request.app.state.templates
    if isinstance(templates, Jinja2Templates):
        return templates
    raise RuntimeError("Template engine is not configured on application state.")


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


async def _render_website_detail(
    *,
    request: Request,
    session: AsyncSession,
    website_id: UUID,
    feedback: str | None,
) -> Response:
    templates = _get_templates(request)
    context = await build_website_detail_context(
        session=session,
        website_id=website_id,
        feedback=feedback,
    )
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


@router.get("/", response_class=Response)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    templates = _get_templates(request)
    metrics = await _fetch_dashboard_metrics(request=request, session=session)
    activities = await _fetch_recent_activity(session=session, limit=20)
    system_status = await _build_system_status_context(request=request, session=session)
    return templates.TemplateResponse(
        request=request,
        name="pages/dashboard.html",
        context={
            "page_title": "Dashboard",
            "metrics": metrics,
            "activities": activities,
            "running_jobs": system_status["running_jobs"],
            "last_completed_runs": system_status.get("last_completed_runs", {}),
            "next_scheduled_runs": system_status["next_scheduled_runs"],
            "refresh_trigger": system_status["refresh_trigger"],
        },
    )


@router.get("/web/partials/dashboard-stats", response_class=HTMLResponse)
async def dashboard_stats_partial(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    templates = _get_templates(request)
    metrics = await _fetch_dashboard_metrics(request=request, session=session)
    return templates.TemplateResponse(
        request=request,
        name="partials/dashboard_stats.html",
        context={"metrics": metrics},
    )


@router.get("/web/partials/activity-feed", response_class=HTMLResponse)
async def dashboard_activity_feed_partial(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    templates = _get_templates(request)
    return templates.TemplateResponse(
        request=request,
        name="partials/activity_feed.html",
        context={"activities": await _fetch_recent_activity(session=session, limit=20)},
    )


@router.get("/web/partials/system-status", response_class=HTMLResponse)
async def dashboard_system_status_partial(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    templates = _get_templates(request)
    system_ctx = await _build_system_status_context(request=request, session=session)
    return templates.TemplateResponse(
        request=request,
        name="partials/system_status.html",
        context=system_ctx,
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
    low_priority_count = int(
        (
            await session.scalar(
                select(func.count()).select_from(URL).where(
                    URL.current_priority > 0,
                    URL.current_priority < 0.8,
                )
            )
        )
        or 0
    )

    # Fetch ETA data
    eta_service = QueueETAService(session)
    eta_data = await eta_service.get_all_websites_eta()

    # Get quota reset time (same for all websites)
    quota_reset_at = eta_data[0].quota_reset_at if eta_data else None

    # Convert to dict format for template
    websites_eta = []
    for eta in eta_data:
        websites_eta.append(
            {
                "website_id": str(eta.website_id),
                "website_domain": eta.website_domain,
                "status": eta.status,
                "submission_queue": {
                    "queued": eta.submission_queue.queued,
                    "quota_remaining": eta.submission_queue.quota_remaining,
                    "quota_limit": eta.submission_queue.quota_limit,
                    "eta_minutes": eta.submission_queue.eta_minutes,
                    "rate_per_minute": round(eta.submission_queue.rate_per_minute, 1),
                },
                "verification_queue": {
                    "queued": eta.verification_queue.queued,
                    "quota_remaining": eta.verification_queue.quota_remaining,
                    "quota_limit": eta.verification_queue.quota_limit,
                    "eta_minutes": eta.verification_queue.eta_minutes,
                    "rate_per_minute": round(eta.verification_queue.rate_per_minute, 1),
                },
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="partials/queue_status.html",
        context={
            "queued_count": queued_count,
            "high_priority_count": high_priority_count,
            "low_priority_count": low_priority_count,
            "websites_eta": websites_eta,
            "quota_reset_at": quota_reset_at,
        },
    )


@router.get("/ui/queue", response_class=Response)
async def queue_management(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
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
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
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
    _: UserInfo = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    form_data = await request.form()
    priority = _form_float(form_data.get("priority"), default=0.5)
    page = _form_int(form_data.get("page"), default=1)
    page_size = _form_int(form_data.get("page_size"), default=DEFAULT_PAGE_SIZE)
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
    _: UserInfo = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    form_data = await request.form()
    action = str(form_data.get("action") or "")
    page = _form_int(form_data.get("page"), default=1)
    page_size = _form_int(form_data.get("page_size"), default=DEFAULT_PAGE_SIZE)
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


@router.get("/ui/websites/{website_id}/urls", response_class=Response)
async def website_urls_page(
    request: Request,
    website_id: UUID,
    status_filter: URLIndexStatus | None = Query(default=None, alias="status"),
    sitemap_id: UUID | None = Query(default=None),
    search: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    templates = _get_templates(request)
    await ensure_website_exists(session=session, website_id=website_id)
    website = await session.get(Website, website_id)
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found",
        )

    sitemap_options = await list_website_sitemaps(
        session=session, website_id=website_id
    )
    listing: WebsiteURLListResponse = await fetch_website_urls(
        session=session,
        website_id=website_id,
        status_filter=status_filter,
        sitemap_id=sitemap_id,
        search=search,
        page=page,
        page_size=page_size,
        include_all=False,
    )
    return templates.TemplateResponse(
        request=request,
        name="pages/website_urls.html",
        context={
            "page_title": f"{website.domain} URL Index Status",
            "website": website,
            "listing": listing,
            "sitemap_options": sitemap_options,
            "status_options": list(URLIndexStatus),
            "filters": {
                "status": status_filter,
                "sitemap_id": sitemap_id,
                "search": search,
                "page_size": page_size,
            },
        },
    )


@router.post(
    "/ui/websites", response_class=Response, status_code=status.HTTP_201_CREATED
)
async def create_website_from_web(
    request: Request,
    _: UserInfo = Depends(require_admin),
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
    _: UserInfo = Depends(require_admin),
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
    _: UserInfo = Depends(require_admin),
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
    _: UserInfo = Depends(require_admin),
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
        await _activity_service.log_activity(
            session=session,
            event_type="sitemap_added",
            website_id=website_id,
            resource_type="sitemap",
            resource_id=sitemap.id,
            message=f"Sitemap added: {sitemap.url}",
            metadata={"sitemap_type": sitemap.sitemap_type.value},
        )
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
    _: UserInfo = Depends(require_admin),
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
    await _activity_service.log_activity(
        session=session,
        event_type="sitemap_removed",
        website_id=website_id,
        resource_type="sitemap",
        resource_id=sitemap.id,
        message=f"Sitemap removed: {sitemap.url}",
        metadata={"sitemap_type": sitemap.sitemap_type.value},
    )
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
    _: UserInfo = Depends(require_admin),
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
    service = TriggerIndexingService(
        session,
        discovery_service=discovery_service,
        queue_service=queue_service,
    )

    try:
        result = await service.trigger_indexing(website_id)
        return await _render_website_detail(
            request=request,
            session=session,
            website_id=website_id,
            feedback=result.feedback,
        )
    except SitemapFetchException as error:
        _trigger_logger.error(
            {
                "event": "trigger_indexing_failed",
                "website_id": str(website_id),
                "sitemap_id": None,
                "sitemap_url_sanitized": _safe_sitemap_url_for_feedback(error.url),
                "stage": "fetch",
                "exception_class": error.__class__.__name__,
                "http_status": error.status_code,
                "content_type": error.content_type,
            }
        )
        feedback = _trigger_feedback_for_fetch_error(
            error=error.original_error,  # type: ignore[arg-type]
            safe_sitemap_url=_safe_sitemap_url_for_feedback(error.url),
        )
        return await _rollback_and_render_website_detail(
            request=request,
            session=session,
            website_id=website_id,
            feedback=feedback,
        )
    except URLDiscoveryProcessingException as error:
        _trigger_logger.error(
            {
                "event": "trigger_indexing_failed",
                "website_id": str(website_id),
                "sitemap_id": str(error.sitemap_id),
                "sitemap_url_sanitized": _safe_sitemap_url_for_feedback(
                    error.sitemap_url
                ),
                "stage": error.stage,
                "exception_class": error.__class__.__name__,
                "http_status": error.status_code,
                "content_type": error.content_type,
            }
        )
        feedback = _trigger_feedback_for_discovery_error(
            error=error,
            safe_sitemap_url=_safe_sitemap_url_for_feedback(error.sitemap_url),
        )
        return await _rollback_and_render_website_detail(
            request=request,
            session=session,
            website_id=website_id,
            feedback=feedback,
        )
    except EnqueueException as error:
        sitemap_url_row = (
            await session.execute(
                select(Sitemap.url)
                .where(
                    Sitemap.website_id == website_id,
                    Sitemap.is_active.is_(True),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        enqueue_context_sitemap_url = (
            _safe_sitemap_url_for_feedback(sitemap_url_row) if sitemap_url_row else None
        )
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


@router.post("/websites/{website_id}/quota/discover", response_class=Response)
@router.post("/ui/websites/{website_id}/quota/discover", response_class=Response)
async def discover_quota_from_web(
    request: Request,
    website_id: UUID,
    _: UserInfo = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    website = await session.get(Website, website_id)
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found",
        )

    await _quota_discovery_service.discover_quota(
        session=session, website_id=website_id
    )
    return await _render_website_detail(
        request=request,
        session=session,
        website_id=website_id,
        feedback="Quota discovery started",
    )


@router.get("/web/websites/{website_id}/quota-edit", response_class=HTMLResponse)
@router.get("/ui/websites/{website_id}/quota-edit", response_class=HTMLResponse)
async def quota_edit_form(
    request: Request,
    website_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    """Get the quota edit form."""
    templates = _get_templates(request)
    website = await session.get(Website, website_id)
    if website is None:
        raise HTTPException(status_code=404, detail="Website not found")

    # Get current quota status
    quota_service = QuotaDiscoveryService()
    quota_status = await quota_service.get_discovered_limits(session, website_id)

    return templates.TemplateResponse(
        request=request,
        name="partials/quota_edit_form.html",
        context={
            "website": website,
            "quota_status": quota_status,
        },
    )


@router.post("/web/websites/{website_id}/quota-override", response_class=HTMLResponse)
@router.post("/ui/websites/{website_id}/quota-override", response_class=HTMLResponse)
async def set_quota_override(
    request: Request,
    website_id: UUID,
    _: UserInfo = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    """Handle quota override form submission."""
    templates = _get_templates(request)
    form = await request.form()

    indexing_limit = _form_int(form.get("indexing_limit"), default=0)
    inspection_limit = _form_int(form.get("inspection_limit"), default=0)
    indexing_used = _form_int(form.get("indexing_used"), default=0)
    inspection_used = _form_int(form.get("inspection_used"), default=0)
    mode = str(form.get("mode", "manual"))

    website = await session.get(Website, website_id)
    if website is None:
        raise HTTPException(status_code=404, detail="Website not found")

    now = datetime.now(UTC)
    today = now.date()

    # Get or create today's usage record
    usage = await session.execute(
        select(QuotaUsage).where(
            QuotaUsage.website_id == website_id,
            QuotaUsage.date == today,
        )
    )
    usage_row = usage.scalar_one_or_none()

    if usage_row is None:
        usage_row = QuotaUsage(
            website_id=website_id,
            date=today,
            indexing_count=0,
            inspection_count=0,
        )
        session.add(usage_row)

    # Update limits if provided
    if indexing_limit is not None and indexing_limit > 0:
        website.discovered_indexing_quota = indexing_limit
    if inspection_limit is not None and inspection_limit > 0:
        website.discovered_inspection_quota = inspection_limit

    # Update used counts if provided
    if indexing_used is not None and indexing_used >= 0:
        usage_row.indexing_count = indexing_used
    if inspection_used is not None and inspection_used >= 0:
        usage_row.inspection_count = inspection_used

    # Set mode
    if mode == "auto":
        website.quota_discovery_status = QuotaDiscoveryStatus.DISCOVERING
    else:
        website.quota_discovery_status = QuotaDiscoveryStatus.CONFIRMED
        # Mark quota as discovered so it doesn't restart automatically
        website.quota_discovered_at = datetime.now(UTC)

    await session.commit()

    # Get updated quota status
    quota_service = QuotaDiscoveryService()
    quota_status = await quota_service.get_discovered_limits(session, website_id)

    return templates.TemplateResponse(
        request=request,
        name="partials/quota_edit_form.html",
        context={
            "website": website,
            "quota_status": quota_status,
            "saved": True,
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
    _: UserInfo = Depends(require_admin),
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


@router.post(
    "/ui/websites/{website_id}/urls/{url_id}/inspect",
    response_class=HTMLResponse,
)
async def inspect_single_url(
    request: Request,
    website_id: UUID,
    url_id: UUID,
    _: UserInfo = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    """Inspect a single URL via Google URL Inspection API, bypassing rate limits."""
    templates = _get_templates(request)

    website = await session.get(Website, website_id)
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found",
        )

    result = await inspect_single_url_service(session, website_id, url_id)

    if not result.success:
        return templates.TemplateResponse(
            request=request,
            name="partials/website_url_row.html",
            context={
                "website": website,
                "item": result.item,
                "error_message": result.error_message,
            },
        )

    return templates.TemplateResponse(
        request=request,
        name="partials/website_url_row.html",
        context={
            "website": website,
            "item": result.item,
        },
    )


@router.post(
    "/ui/websites/{website_id}/urls/{url_id}/submit",
    response_class=HTMLResponse,
)
async def submit_single_url(
    request: Request,
    website_id: UUID,
    url_id: UUID,
    _: UserInfo = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    """Submit a single URL via Google Indexing API, bypassing rate limits."""
    templates = _get_templates(request)

    website = await session.get(Website, website_id)
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found",
        )

    result = await submit_single_url_service(session, website_id, url_id)

    if result.error_type == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=result.error_message,
        )

    return templates.TemplateResponse(
        request=request,
        name="partials/website_url_row.html",
        context={
            "website": website,
            "item": result.item,
            "error_message": result.error_message if not result.success else None,
        },
    )
