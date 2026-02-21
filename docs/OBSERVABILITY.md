# Observability Guide

This guide covers the observability features added across the indexing pipeline and dashboard.

## Index Status Tracking and URL Drill-Down

- Each tracked URL keeps a denormalized latest index state (`INDEXED`, `NOT_INDEXED`, `BLOCKED`, `SOFT_404`, `ERROR`, `UNCHECKED`) for fast filtering.
- `GET /api/websites/{website_id}/index-stats` returns website-level totals, coverage percentage, and sitemap breakdown.
- `GET /api/dashboard/index-stats` returns cross-website totals and per-website coverage.
- `GET /api/websites/{website_id}/urls` supports:
  - status filtering
  - sitemap filtering
  - text search by URL substring
  - pagination
- `GET /api/websites/{website_id}/urls/export` exports the filtered result set as CSV.

Example:

```bash
curl "http://localhost:8000/api/websites/<website-id>/urls?status=NOT_INDEXED&search=blog&page=1&page_size=50"
```

```bash
curl "http://localhost:8000/api/websites/<website-id>/urls/export?status=ERROR" -o urls.csv
```

## Dynamic Quota Discovery

- Quotas are discovered and refined per website from observed API behavior.
- Discovery starts with conservative defaults and adjusts confidence over time.
- 429 handling reduces confidence and quota estimates; retry-after responses apply a smaller confidence penalty.
- Quota state includes:
  - discovered indexing/inspection limits
  - confidence score
  - discovery status (`pending`, `discovering`, `estimated`, `confirmed`, `failed`)
  - timestamps for last discovery and last 429

Example:

```bash
curl "http://localhost:8000/api/websites/<website-id>/quota"
```

## Real-Time Processing Status

- Scheduler runtime status is exposed at `GET /api/scheduler`.
- Job-level runtime metrics are available at `GET /api/scheduler/jobs/monitoring`.
- Persisted execution history is available at `GET /api/scheduler/jobs/history`.
- Dashboard partial widgets refresh processing and scheduling data for active operations.

Example:

```bash
curl "http://localhost:8000/api/scheduler/jobs/history?status=failed&page=1&page_size=20"
```

## Activity Log

- Cross-cutting events are persisted in `activity_logs`.
- API access: `GET /api/activity` with optional filters:
  - `event_type`
  - `website_id`
  - `date_from`
  - `date_to`
  - pagination controls

Example:

```bash
curl "http://localhost:8000/api/activity?event_type=quota_discovered&page=1&page_size=20"
```

## Crash Recovery Behavior

- Running jobs are detected during startup recovery.
- Interrupted jobs are marked failed with checkpoint metadata for forensic review.
- Startup and shutdown summaries emit structured lifecycle logs, including queue size, status counters, and interrupted job counts.
- Recovery updates are persisted so post-crash UI/API state reflects reality instead of stale in-memory execution state.

## Screenshot Placeholders

Add screenshots in `docs/images/` and reference them here once available:

- `docs/images/dashboard-index-coverage.png` - Dashboard with index coverage widgets.
- `docs/images/url-drilldown-filters.png` - URL drill-down page with filtering and pagination.
- `docs/images/quota-status-display.png` - Quota discovery status and confidence display.
- `docs/images/activity-feed.png` - Dashboard activity feed with recent events.
