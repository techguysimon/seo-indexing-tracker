"""Dashboard rendering service for the web UI layer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.config import get_settings
from seo_indexing_tracker.models import (
    ActivityLog,
    IndexStatus,
    JobExecution,
    QuotaUsage,
    SubmissionLog,
    SubmissionStatus,
    URL,
    Website,
)
from seo_indexing_tracker.services.index_stats_service import IndexStatsService
from seo_indexing_tracker.services.processing_pipeline import (
    INDEX_VERIFICATION_JOB_ID,
    SITEMAP_REFRESH_JOB_ID,
    URL_SUBMISSION_JOB_ID,
)


def _format_next_run_time(next_run: datetime | None) -> str:
    """Format a next_run_time as relative time or US date."""
    if next_run is None:
        return "Paused"

    now = datetime.now(UTC)
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=UTC)

    eastern_tz = ZoneInfo("America/New_York")
    now_eastern = now.astimezone(eastern_tz)
    value_eastern = next_run.astimezone(eastern_tz)
    delta = value_eastern - now_eastern

    if delta < timedelta(0):
        return "overdue"
    if delta < timedelta(minutes=1):
        return "in <1m"
    if delta < timedelta(hours=1):
        minutes = int(delta.total_seconds() / 60)
        return f"in {minutes}m"
    if delta < timedelta(days=1):
        hours = int(delta.total_seconds() / 3600)
        return f"in {hours}h"

    return value_eastern.strftime("%-m-%-d %-I:%M %p")


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
        job.job_id: _format_next_run_time(job.next_run_time)
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

    pipeline_pulse = await _fetch_pipeline_pulse(session=session)

    return {
        "queued_urls": queued_urls,
        "manual_overrides": manual_overrides,
        "active_websites": active_websites,
        "tracked_urls": tracked_urls,
        "queue_by_website": queue_by_website,
        "index_stats": index_stats,
        "pipeline_pulse": pipeline_pulse,
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

    # Fetch last completed run for each job type
    last_completed_runs = await _fetch_last_completed_runs(session=session)

    return {
        "running_jobs": running_jobs,
        "last_completed_runs": last_completed_runs,
        "next_scheduled_runs": _format_next_run_by_job(request),
        "refresh_trigger": "load, every 10s" if has_running_jobs else "load, every 30s",
    }


async def _fetch_last_completed_runs(
    *, session: AsyncSession,
) -> dict[str, dict[str, object]]:
    """Return last completed execution metadata for each pipeline job."""
    job_ids = [URL_SUBMISSION_JOB_ID, INDEX_VERIFICATION_JOB_ID, SITEMAP_REFRESH_JOB_ID]
    result: dict[str, dict[str, object]] = {}
    for job_id in job_ids:
        row = (
            await session.execute(
                select(JobExecution)
                .where(
                    JobExecution.job_id == job_id,
                    JobExecution.status == "completed",
                )
                .order_by(JobExecution.finished_at.desc())
                .limit(1)
            )
        )
        execution = row.scalars().first()
        if execution is not None and execution.finished_at is not None:
            result[job_id] = {
                "finished_at": execution.finished_at,
                "urls_processed": execution.urls_processed,
                "duration_seconds": (
                    (execution.finished_at - execution.started_at).total_seconds()
                    if execution.started_at
                    else None
                ),
            }
    return result


async def _fetch_pipeline_pulse(
    *, session: AsyncSession,
) -> dict[str, object]:
    """Aggregate today's submission/verification counts, rates, and quota headroom."""
    settings = get_settings()
    today = datetime.now(UTC).date()
    today_start = datetime.combine(today, datetime.min.time(), tzinfo=UTC)
    one_hour_ago = datetime.now(UTC) - timedelta(hours=1)

    # Submissions today (SUCCESS only)
    submissions_today = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(SubmissionLog)
                .where(
                    SubmissionLog.status == SubmissionStatus.SUCCESS,
                    SubmissionLog.submitted_at >= today_start,
                )
            )
        )
        or 0
    )

    # Submissions in last hour
    submissions_last_hour = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(SubmissionLog)
                .where(
                    SubmissionLog.status == SubmissionStatus.SUCCESS,
                    SubmissionLog.submitted_at >= one_hour_ago,
                )
            )
        )
        or 0
    )

    # Verifications today (IndexStatus records created today)
    verifications_today = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(IndexStatus)
                .where(IndexStatus.checked_at >= today_start)
            )
        )
        or 0
    )

    # Verifications in last hour
    verifications_last_hour = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(IndexStatus)
                .where(IndexStatus.checked_at >= one_hour_ago)
            )
        )
        or 0
    )

    # Aggregate quota across all active websites
    total_indexing_limit = 0
    total_indexing_used = 0
    total_inspection_limit = 0
    total_inspection_used = 0

    websites_result = await session.execute(
        select(Website).where(Website.is_active.is_(True))
    )
    websites = websites_result.scalars().all()

    for website in websites:
        indexing_limit = int(
            website.discovered_indexing_quota
            if website.discovered_indexing_quota is not None
            else settings.INDEXING_DAILY_QUOTA_LIMIT
        )
        inspection_limit = int(
            website.discovered_inspection_quota
            if website.discovered_inspection_quota is not None
            else settings.INSPECTION_DAILY_QUOTA_LIMIT
        )
        total_indexing_limit += indexing_limit
        total_inspection_limit += inspection_limit

        quota_row = await session.execute(
            select(QuotaUsage).where(
                QuotaUsage.website_id == website.id,
                QuotaUsage.date == today,
            )
        )
        usage = quota_row.scalar_one_or_none()
        if usage is not None:
            total_indexing_used += int(usage.indexing_count)
            total_inspection_used += int(usage.inspection_count)

    return {
        "submissions_today": submissions_today,
        "submissions_per_hour": submissions_last_hour,
        "verifications_today": verifications_today,
        "verifications_per_hour": verifications_last_hour,
        "indexing_quota_used": total_indexing_used,
        "indexing_quota_limit": total_indexing_limit,
        "indexing_quota_remaining": max(total_indexing_limit - total_indexing_used, 0),
        "inspection_quota_used": total_inspection_used,
        "inspection_quota_limit": total_inspection_limit,
        "inspection_quota_remaining": max(
            total_inspection_limit - total_inspection_used, 0
        ),
    }
