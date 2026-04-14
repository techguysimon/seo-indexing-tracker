# AI Agent Guide - SEO Indexing Tracker

## Project Overview

SEO Indexing Tracker is a FastAPI-based service for monitoring website sitemap coverage and managing URL indexing workflows through Google APIs. The system discovers URLs from sitemaps, submits them to Google's Indexing API, and verifies actual index status using the Search Console URL Inspection API.

**Key Capabilities:**
- Multi-website support with per-site service account quota isolation
- Automatic sitemap discovery and parsing with `<lastmod>` change detection
- Dual-API strategy: Indexing API for submissions, URL Inspection API for verification
- Priority-based URL queue with manual override support
- Web UI dashboard with queue management and statistics
- SQLite-backed persistence with WAL mode for crash-safety

## Tech Stack Summary

| Layer | Technology | Version |
|:---|:---|:---|
| Language | Python | 3.11+ |
| Framework | FastAPI | 0.115+ |
| Database | SQLite (WAL mode) | with aiosqlite |
| Frontend | HTMX + Jinja2 | - |
| Scheduling | APScheduler | 3.10+ |
| HTTP Client | httpx | 0.28+ |
| XML Parsing | lxml | 5.3+ |
| Google APIs | google-api-python-client | 2.149+ |
| Package Manager | uv | latest |

## Project Structure

```
seo_indexing_tracker/
├── src/seo_indexing_tracker/
│   ├── main.py              # FastAPI app factory and entry point
│   ├── config.py            # Settings from environment variables
│   ├── database.py          # SQLAlchemy engine, session management
│   ├── api/                 # API route modules
│   │   ├── websites.py      # Website CRUD endpoints
│   │   ├── sitemaps.py      # Sitemap management endpoints
│   │   ├── service_accounts.py  # Service account management
│   │   ├── queue.py         # Priority queue management
│   │   ├── scheduler.py     # Scheduler job control
│   │   ├── web.py           # Web UI routes
│   │   └── config_validation.py  # Settings validation
│   ├── services/            # Business logic layer
│   │   ├── scheduler.py                # APScheduler wrapper
│   │   ├── processing_pipeline.py      # Scheduled job execution (3 core jobs)
│   │   ├── job_runner.py               # Job execution with overlap protection
│   │   ├── job_recovery_service.py     # Crash recovery for interrupted jobs
│   │   ├── priority_queue.py           # URL priority queue
│   │   ├── url_discovery.py            # Sitemap URL extraction
│   │   ├── url_submission_service.py   # Indexing API submissions
│   │   ├── url_inspection_service.py   # URL Inspection API verification
│   │   ├── url_item_builder.py         # URL record construction
│   │   ├── trigger_indexing_service.py # Manual trigger workflows
│   │   ├── batch_processor.py          # Batch submission/verification
│   │   ├── rate_limiter.py             # API rate limiting (semaphores)
│   │   ├── cooldown_service.py         # Rate limit cooldown tracking
│   │   ├── quota_service.py            # Per-website quota tracking
│   │   ├── quota_discovery_service.py  # Quota auto-discovery from APIs
│   │   ├── google_api_factory.py       # Google API client factory
│   │   ├── google_credentials.py       # Service account credential loading
│   │   ├── google_indexing_client.py   # Indexing API wrapper
│   │   ├── google_url_inspection_client.py  # URL Inspection API wrapper
│   │   ├── google_errors.py            # Google API error classification
│   │   ├── sitemap_fetcher.py          # Sitemap HTTP fetch + decompression
│   │   ├── sitemap_decompressor.py     # Gzip/content-encoding handling
│   │   ├── sitemap_index_parser.py     # Sitemap index XML parsing
│   │   ├── sitemap_url_parser.py       # URL sitemap XML parsing
│   │   ├── sitemap_type_detector.py    # Sitemap type classification
│   │   ├── config_validation.py        # Settings/sitemap URL validation
│   │   ├── index_stats_service.py      # Index coverage statistics
│   │   ├── queue_eta_service.py        # Queue ETA calculations
│   │   ├── queue_template_service.py   # Queue table template data
│   │   ├── dashboard_service.py        # Dashboard template data
│   │   ├── website_detail_service.py   # Website detail page data
│   │   ├── activity_service.py         # Activity log queries
│   │   └── auth_service.py             # Google OAuth + JWT auth
│   ├── schemas/             # Pydantic models for API payloads
│   ├── models/              # SQLAlchemy ORM models
│   │   ├── base.py                      # Base model
│   │   ├── website.py                   # Website records
│   │   ├── url.py                       # Discovered URLs
│   │   ├── sitemap.py                   # Sitemap records
│   │   ├── sitemap_refresh_progress.py  # Sitemap fetch progress
│   │   ├── service_account.py           # Service account credentials
│   │   ├── index_status.py              # Index verification results
│   │   ├── submission_log.py            # API submission history
│   │   ├── quota_usage.py               # Daily quota counters
│   │   ├── rate_limit_state.py          # Rate limiter state
│   │   ├── job_execution.py             # Job run tracking
│   │   └── activity_log.py              # Activity feed events
│   ├── utils/              # Utility helpers
│   ├── templates/          # Jinja2 HTML templates (IndexPulse design)
│   └── static/             # CSS, JS assets
├── docs/                   # Architecture documentation
├── tests/                  # Test suite
├── pyproject.toml          # Project configuration
└── Dockerfile              # Container definition
```

## Development Setup

### Prerequisites

- Python 3.11 or higher
- uv package manager (recommended)

### Install Dependencies

```bash
# Install uv if needed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install project with dev dependencies
uv sync --extra dev
```

### Run the Application

```bash
# Development server with auto-reload
uv run uvicorn seo_indexing_tracker.main:app --reload --host 0.0.0.0 --port 8000

# Production mode
uv run seo-indexing-tracker
```

### Run Tests

```bash
# Run all tests with coverage
uv run pytest

# Run specific test file
uv run pytest tests/test_file.py

# Run with verbose output
uv run pytest -v
```

### Code Quality Tools

```bash
# Lint with ruff
uv run ruff check .
uv run ruff check src tests

# Auto-fix linting issues
uv run ruff check --fix .

# Format code
uv run ruff format .

# Type checking with mypy
uv run mypy src tests
```

## Common Tasks

### Adding a New API Endpoint

1. Create or update the schema in `src/seo_indexing_tracker/schemas/`
2. Add route handler in appropriate `src/seo_indexing_tracker/api/` module
3. Register router in `src/seo_indexing_tracker/main.py`
4. Add tests in `tests/`

### Adding a Scheduled Job

1. Define job function in `src/seo_indexing_tracker/services/processing_pipeline.py`
2. Register job in `SchedulerProcessingPipelineService.register_jobs()`
3. Job automatically gets overlap protection and metrics tracking

### UI Design System

The UI follows the **IndexPulse** design system ("Slate Teal Precision"). All templates use Tailwind CSS (CDN), Manrope/Inter fonts, and Material Symbols Outlined icons.

- **Design spec:** `docs/INDEXPULSE_DESIGN_SYSTEM.md` — colors, typography, component rules, "no-line" philosophy
- **Tailwind config:** Defined inline in `base.html` with the full design system color palette
- **Core layout:** Sidebar nav + glass header + mobile bottom bar (`base.html`)
- **CSS:** Minimal overrides only in `static/css/app.css` (Tailwind handles everything else)

When adding or modifying UI:
1. Use the design system color tokens (`primary`, `surface-container-lowest`, `on-surface`, etc.) — not raw hex
2. Use `font-headline` for headings, `font-body` for data, `font-label` for uppercase metadata
3. Use Material Symbols (`<span class="material-symbols-outlined">icon_name</span>`) for icons
4. No `1px solid` borders — use tonal background shifts (`surface-container-low` on `background`)
5. Preserve all HTMX attributes (`hx-get`, `hx-post`, `hx-target`, `hx-swap`) and Jinja2 syntax

### Updating the UI Setup Flow

The full setup flow is centered on the website detail page:

1. `src/seo_indexing_tracker/templates/pages/websites.html` - website list/create and link into detail view
2. `src/seo_indexing_tracker/templates/pages/website_detail.html` - detail shell
3. `src/seo_indexing_tracker/templates/partials/website_detail_panel.html` - service account, sitemap CRUD, trigger indexing UI
4. `src/seo_indexing_tracker/api/web.py` - `/ui/websites/{id}` setup actions and trigger indexing handlers

Queue page rendering relies on:

- `src/seo_indexing_tracker/templates/pages/queue.html`
- `src/seo_indexing_tracker/templates/partials/queue_table.html`

Keep page/partial context keys aligned when changing queue filters, pagination, or batch actions.

### Database Migrations

The project uses SQLAlchemy's `Base.metadata.create_all()` for schema creation. For new models:

1. Add model class to `src/seo_indexing_tracker/models/`
2. Import and reference in `src/seo_indexing_tracker/models/__init__.py`
3. Database is created automatically on startup

### Environment Configuration

All settings are defined in `src/seo_indexing_tracker/config.py` and loaded from `.env`:

| Variable | Description | Default |
|:---|:---|:---|
| `DATABASE_URL` | SQLAlchemy database URL | `sqlite+aiosqlite:///./data/seo_indexing_tracker.db` |
| `SECRET_KEY` | Application secret key | (required) |
| `HOST` | Server host | `0.0.0.0` |
| `PORT` | Server port | `8000` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |
| `LOG_FORMAT` | Log output format (`json` or `text`) | `text` |
| `LOG_FILE` | Optional log file path | None |
| `OUTBOUND_HTTP_USER_AGENT` | User-Agent used for outbound sitemap/config validation HTTP requests | `BlueBeastBuildAgent` |
| `SCHEDULER_ENABLED` | Enable scheduler jobs | `true` |
| `SCHEDULER_JOBSTORE_URL` | APScheduler jobstore database URL | `sqlite:///./scheduler-jobs.sqlite` |
| `SCHEDULER_URL_SUBMISSION_INTERVAL_SECONDS` | URL submission frequency | `300` |
| `SCHEDULER_URL_SUBMISSION_BATCH_SIZE` | URLs per submission batch | `100` |
| `SCHEDULER_INDEX_VERIFICATION_INTERVAL_SECONDS` | Verification frequency | `900` |
| `SCHEDULER_INDEX_VERIFICATION_BATCH_SIZE` | URLs per verification batch | `100` |
| `SCHEDULER_INDEXED_REVERIFICATION_MIN_AGE_SECONDS` | Min age before re-verifying indexed URLs (default 7 days) | `604800` |
| `SCHEDULER_SITEMAP_REFRESH_INTERVAL_SECONDS` | Sitemap refresh frequency | `3600` |
| `INDEXING_DAILY_QUOTA_LIMIT` | Default daily Indexing API quota per website | `200` |
| `INSPECTION_DAILY_QUOTA_LIMIT` | Default daily Inspection API quota per website | `2000` |
| `QUOTA_RATE_LIMIT_COOLDOWN_SECONDS` | Cooldown after hitting rate limit | `3600` |
| `JOB_RECOVERY_AUTO_RESUME` | Auto-resume interrupted jobs on startup | `false` |
| `SHUTDOWN_GRACE_PERIOD_SECONDS` | Grace period for in-flight jobs on shutdown | `30` |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID (enables auth) | `""` |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret | `""` |
| `ADMIN_EMAILS` | Comma-separated admin email addresses | `""` |
| `GUEST_EMAILS` | Comma-separated guest email addresses | `""` |
| `JWT_SECRET_KEY` | JWT signing key for session tokens | `""` |
| `JWT_EXPIRY_HOURS` | JWT token lifetime in hours | `24` |

`python-multipart` is required for form parsing in web UI routes (`request.form()`). It is installed via project dependencies.

### Authentication

When `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are configured, the app uses Google OAuth for login. Users are classified as:

- **Admin** (`ADMIN_EMAILS`): Full access — CRUD websites, sitemaps, service accounts, queue management, trigger indexing
- **Guest** (`GUEST_EMAILS`): Read-only access — dashboard, queue view, URL inspection
- **Unauthenticated**: When auth is not configured, all routes are open (development mode)

JWT tokens are issued on login (`JWT_SECRET_KEY`, `JWT_EXPIRY_HOURS`). Without `GOOGLE_CLIENT_ID` set, the app runs in unauthenticated mode.

## Architectural Patterns

### Async/Await

All I/O operations use async/await with SQLAlchemy async support and httpx async client. Use `async with session_scope()` for database transactions.

### Dependency Injection

FastAPI dependencies are used for request-scoped resources:

```python
async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session
```

### Service Layer Pattern

Business logic resides in `services/` modules. API routes delegate to services, keeping route handlers thin.

### Indexing Pipeline (3 Core Jobs)

The processing pipeline (`services/processing_pipeline.py`) runs three scheduled jobs:

1. **Sitemap Refresh** (`sitemap-refresh-job`, default 1h): Discovers new/modified URLs from registered sitemaps. Parses XML, detects `<lastmod>` changes, enqueues new URLs.

2. **URL Submission** (`url-submission-job`, default 5m): Dequeues highest-priority URLs and submits them to Google's Indexing API. Respects per-website quota limits and rate limiting.

3. **Index Verification** (`index-verification-job`, default 15m): Queries the URL Inspection API to verify actual index status. Re-verification skips recently-checked URLs (7-day min age by default).

All jobs use `job_runner.py` for overlap protection (prevents concurrent runs of the same job) and `job_recovery_service.py` for crash recovery.

### Jinja2 Template Filters

Registered in `main.py`, available in all templates:

| Filter | Usage | Example |
|:---|:---|:---|
| `datetime_us` | Full US datetime | `{{ event.created_at \| datetime_us }}` → `4-13-2026 1:58 PM` |
| `datetime_relative` | Relative time | `{{ item.updated_at \| datetime_relative }}` → `5 mins ago` |
| `humanize_date` | Alias for `datetime_relative` | `{{ item.last_crawl_at \| humanize_date }}` |

All filters convert UTC to Eastern Time automatically.

### Rate Limiting

Per-website rate limiting is enforced via `RateLimiterService` using semaphores. Each website has independent quota tracking.

### Priority Queue

URLs are processed based on priority (manual override or auto-calculated from `lastmod` age). Higher priority URLs are dequeued first.

### Sitemap Fetching and Child Traversal Constraints

Treat sitemap fetch policy as security-sensitive code:

- Use configured outbound UA (`OUTBOUND_HTTP_USER_AGENT`) for sitemap/config validation calls.
- Preserve hardened gzip/content-encoding handling and 403 retry behavior in `services/sitemap_fetcher.py`.
- Preserve strict child sitemap SSRF protections in `services/url_discovery.py`:
  - child URL policy validation
  - explicit redirect handling with per-hop validation
  - pinned connect IPs with validated fallback IP retries
  - fail-closed behavior if connect destination metadata is unavailable
- Keep trigger diagnostics aligned with runtime behavior: detailed stage metadata in logs, category-level UI feedback (`fetch`/`parse`/`discovery`/`enqueue`), and URL sanitization via `sitemap_url_sanitized`.

## Crash Recovery Guarantees

### What is Preserved
- **URLs**: All discovered URLs and their metadata (lastmod, priority)
- **IndexStatus**: All historical index verification results
- **QuotaUsage**: Daily API usage counters
- **Scheduler Jobs**: Job definitions persist via APScheduler's SQLAlchemyJobStore
- **JobExecution**: Job run history and checkpoints
- **RateLimitState**: Token bucket state for rate limiting
- **ActivityLog**: All logged events

### What is Lost on Crash
- In-flight HTTP requests to Google APIs
- Non-checkpointed batch progress (last ~100 URLs in current batch)
- In-memory semaphores (recreated on startup)

### Recovery Behavior
- On restart, scheduler jobs resume from their stored definitions
- Running JobExecution records are marked as "failed" if unfinished
- Batch operations can be resumed from last checkpoint
- Token bucket state is restored from RateLimitState table

## Testing Guidelines

- Tests go in `tests/` directory mirroring `src/` structure
- Use `pytest-asyncio` for async tests
- Use fixtures from `conftest.py` for common test setup
- Mark async tests with `@pytest.mark.asyncio`

For setup-flow/sitemap-security changes, run focused tests before full suite:

- `uv run pytest tests/api/test_web_app_setup.py tests/api/test_web_trigger_indexing.py`
- `uv run pytest tests/services/test_sitemap_fetcher.py tests/services/test_url_discovery.py tests/services/test_config_validation.py`
- `uv run pytest tests/api/test_queue_api.py` when queue template/filter behavior changes

## Docker Usage

```bash
# Build image
docker build -t seo-indexing-tracker .

# Run container
docker run -p 8000:8000 \
  -v ./data:/app/data \
  -v ./service-accounts:/app/service-accounts \
  --env-file .env \
  seo-indexing-tracker
```

## Google API Setup

Each website requires:
1. GCP project with Indexing API and Search Console API enabled
2. Service account with JSON key file
3. Service account added as Owner in Google Search Console
4. Service account added in Webmaster Central

## Additional Resources

- [System Design Document](./SEO_INDEXING_TRACKER_DESIGN.md) - Detailed architecture
- [API Documentation](./docs/) - Endpoint specifications
- [Architecture Docs](./docs/ARCHITECTURE.md) - System architecture
- [ADRs](./docs/DECISIONS.md) - Architectural decision records
