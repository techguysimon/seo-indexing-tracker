"""Dashboard rendering service for the web UI layer."""

from __future__ import annotations

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.models import ActivityLog, JobExecution, URL, Website
from seo_indexing_tracker.services.index_stats_service import IndexStatsService
from seo_indexing_tracker.services.processing_pipeline import (
    INDEX_VERIFICATION_JOB_ID,
    SITEMAP_REFRESH_JOB_ID,
    URL_SUBMISSION_JOB_ID,
)


def _format_next_run_by_job(request: Request) -> dict[str, str]:
    scheduler = getattr(request.app.state, "scheduler_service", None)
    if scheduler is None or not getattr(scheduler, "enabled", False):
        return {
            "url_submission": "Scheduler disabled",
            "index_verification": "Scheduler disabled",
            "sitemap_refresh": "Scheduler disabled",
        }

    try:
        scheduler_jobs = scheduler.list_jobs()
    except RuntimeError:
        return {
            "url_submission": "Unavailable",
            "index_verification": "Unavailable",
            "sitemap_refresh": "Unavailable",
        }

    next_run_by_job_id = {
        job.job_id: job.next_run_time.isoformat() if job.next_run_time else "Paused"
        for job in scheduler_jobs
    }
    return {
        "url_submission": next_run_by_job_id.get(
            URL_SUBMISSION_JOB_ID, "Not registered"
        ),
        "index_verification": next_run_by_job_id.get(
            INDEX_VERIFICATION_JOB_ID,
            "Not registered",
        ),
        "sitemap_refresh": next_run_by_job_id.get(
            SITEMAP_REFRESH_JOB_ID, "Not registered"
        ),
    }


async def _fetch_dashboard_metrics(
    *,
    request: Request,
    session: AsyncSession,
) -> dict[str, object]:
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

    index_stats = await IndexStatsService.get_dashboard_index_stats(session=session)

    return {
        "queued_urls": queued_urls,
        "manual_overrides": manual_overrides,
        "active_websites": active_websites,
        "tracked_urls": tracked_urls,
        "queue_by_website": queue_by_website,
        "index_stats": index_stats,
        "next_scheduled_runs": _format_next_run_by_job(request),
    }


async def _fetch_recent_activity(
    *,
    session: AsyncSession,
    limit: int = 20,
) -> list[dict[str, object]]:
    rows = (
        await session.execute(
            select(ActivityLog, Website.domain)
            .outerjoin(Website, Website.id == ActivityLog.website_id)
            .order_by(ActivityLog.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "id": row[0].id,
            "event_type": row[0].event_type,
            "message": row[0].message,
            "website_id": row[0].website_id,
            "website_domain": row[1] or "Global",
            "created_at": row[0].created_at,
        }
        for row in rows
    ]


async def _build_system_status_context(
    *,
    request: Request,
    session: AsyncSession,
) -> dict[str, object]:
    running_rows = (
        (
            await session.execute(
                select(JobExecution)
                .where(JobExecution.status == "running")
                .order_by(JobExecution.started_at.desc())
            )
        )
        .scalars()
        .all()
    )
    running_jobs = [
        {
            "job_id": row.job_id,
            "job_name": row.job_name,
            "started_at": row.started_at,
            "urls_processed": row.urls_processed,
            "checkpoint_data": row.checkpoint_data,
        }
        for row in running_rows
    ]
    has_running_jobs = bool(running_jobs)
    return {
        "running_jobs": running_jobs,
        "next_scheduled_runs": _format_next_run_by_job(request),
        "refresh_trigger": "load, every 10s" if has_running_jobs else "load, every 30s",
    }
