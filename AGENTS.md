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
│   │   ├── scheduler.py     # APScheduler wrapper
│   │   ├── processing_pipeline.py  # Scheduled job execution
│   │   ├── priority_queue.py # URL priority queue
│   │   ├── url_discovery.py # Sitemap URL extraction
│   │   ├── rate_limiter.py  # API rate limiting
│   │   ├── google_api_factory.py  # Google API client factory
│   │   ├── google_url_inspection_client.py  # URL Inspection API
│   │   └── ...
│   ├── schemas/             # Pydantic models for API payloads
│   ├── models/             # SQLAlchemy ORM models
│   ├── utils/              # Utility helpers
│   ├── templates/          # Jinja2 HTML templates
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
| `OUTBOUND_HTTP_USER_AGENT` | User-Agent used for outbound sitemap/config validation HTTP requests | `BlueBeastBuildAgent` |
| `SCHEDULER_ENABLED` | Enable scheduler jobs | `true` |
| `SCHEDULER_URL_SUBMISSION_INTERVAL_SECONDS` | URL submission frequency | `300` |
| `SCHEDULER_INDEX_VERIFICATION_INTERVAL_SECONDS` | Verification frequency | `900` |
| `SCHEDULER_SITEMAP_REFRESH_INTERVAL_SECONDS` | Sitemap refresh frequency | `3600` |

`python-multipart` is required for form parsing in web UI routes (`request.form()`). It is installed via project dependencies.

Web UI and admin routes are unauthenticated by default. In deployed environments, require external protections (reverse proxy auth, network ACLs, VPN, private ingress, etc.).

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
