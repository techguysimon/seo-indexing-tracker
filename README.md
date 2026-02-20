# SEO Indexing Tracker

SEO Indexing Tracker is a FastAPI-based service for monitoring website sitemap coverage
and tracking indexing-related workflows. It uses a dual-API strategy: the Google Indexing API
for URL submission notifications and the Google Search Console URL Inspection API for
verifying actual index status.

## Features

- Multi-website support with per-site service accounts and quota isolation
- Automatic sitemap discovery and parsing with `<lastmod>` change detection
- Priority-based URL queue with manual override support
- Scheduled jobs for submission, verification, and sitemap refresh
- Web UI dashboard with queue management and statistics
- SQLite-backed persistence with WAL mode for crash-safety

## Project Layout

The core package lives in `src/seo_indexing_tracker/` with the following structure:

- `api/`: API routing modules
- `models/`: SQLAlchemy ORM models
- `services/`: application service layer
- `schemas/`: Pydantic schemas for request and response payloads
- `utils/`: shared utility helpers
- `templates/`: server-side template assets (HTMX + Jinja2)
- `static/`: static frontend assets
- `main.py`: FastAPI app factory and runtime entry point

## Quick Start

### Prerequisites

- Python 3.11 or higher
- uv package manager (recommended)

### Installation

```bash
# Install uv if needed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies and project
uv sync --extra dev
```

### Configuration

Copy `.env.example` to `.env` and configure:

```bash
# Required: Database URL
DATABASE_URL=sqlite+aiosqlite:///./data/seo_indexing_tracker.db

# Required: Application secret key
SECRET_KEY=replace-with-a-long-random-secret
```

> **Tip**: Generate a secure `SECRET_KEY` with:
> ```bash
> python -c "import secrets; print(secrets.token_hex(32))"
> ```

### Run the Application

```bash
# Development server with auto-reload
uv run uvicorn seo_indexing_tracker.main:app --reload --host 0.0.0.0 --port 8000

# Production mode
uv run seo-indexing-tracker
```

### Access the Application

- Web UI: http://localhost:8000
- Health check: http://localhost:8000/health

## Available Commands

### Development

```bash
# Run development server
uv run uvicorn seo_indexing_tracker.main:app --reload --host 0.0.0.0 --port 8000

# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=src --cov-report=html

# Lint code
uv run ruff check .

# Auto-fix linting issues
uv run ruff check --fix .

# Format code
uv run ruff format .

# Type checking
uv run mypy src tests
```

### Using project scripts

```bash
# Run linter
uv run lint

# Format code
uv run format

# Type check
uv run typecheck
```

## Environment Variables Reference

| Variable | Description | Default |
|:---|:---|:---|
| `DATABASE_URL` | SQLAlchemy database URL | `sqlite+aiosqlite:///./data/seo_indexing_tracker.db` |
| `SECRET_KEY` | Application secret key for signing | (required) |
| `HOST` | Server host interface | `0.0.0.0` |
| `PORT` | Server port | `8000` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |
| `LOG_FORMAT` | Log format (`json` or `text`) | `text` |
| `LOG_FILE` | Log file path (empty for stdout) | (stdout) |
| `LOG_FILE_MAX_BYTES` | Log rotation max size | `10485760` |
| `LOG_FILE_BACKUP_COUNT` | Rotated log files to keep | `5` |
| `SCHEDULER_ENABLED` | Enable scheduler jobs | `true` |
| `SCHEDULER_JOBSTORE_URL` | Scheduler job database URL | `sqlite:///./scheduler-jobs.sqlite` |
| `SCHEDULER_URL_SUBMISSION_INTERVAL_SECONDS` | URL submission frequency | `300` |
| `SCHEDULER_INDEX_VERIFICATION_INTERVAL_SECONDS` | Verification frequency | `900` |
| `SCHEDULER_SITEMAP_REFRESH_INTERVAL_SECONDS` | Sitemap refresh frequency | `3600` |
| `SCHEDULER_URL_SUBMISSION_BATCH_SIZE` | URLs per submission batch | `100` |
| `SCHEDULER_INDEX_VERIFICATION_BATCH_SIZE` | URLs per verification batch | `100` |
| `INDEXING_DAILY_QUOTA_LIMIT` | Indexing API daily limit per site | `200` |
| `INSPECTION_DAILY_QUOTA_LIMIT` | Inspection API daily limit per site | `2000` |

## Docker Usage

### Build Image

```bash
docker build -t seo-indexing-tracker .
```

### Run Container

```bash
# Basic run
docker run -p 8000:8000 \
  -v ./data:/app/data \
  -v ./service-accounts:/app/service-accounts \
  --env-file .env \
  seo-indexing-tracker
```

### Docker Compose

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  seo-indexing-tracker:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
      - ./service-accounts:/app/service-accounts
    env_file:
      - .env
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"]
      interval: 30s
      timeout: 10s
      retries: 3
```

Run with Docker Compose:

```bash
docker-compose up -d
```

## Google API Setup

Each website requires its own Google service account for quota isolation:

1. **Create GCP Project**: Create a new Google Cloud Platform project
2. **Enable APIs**: Enable both Indexing API and Search Console API
3. **Create Service Account**: Create a service account and download JSON key
4. **Add to Search Console**: Add the service account email as Owner in Google Search Console
5. **Add to Webmaster Central**: Add the service account email for Indexing API access

### Required API Scopes

- Indexing API: `https://www.googleapis.com/auth/indexing`
- Search Console: `https://www.googleapis.com/auth/webmasters`

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed system architecture.

## API Disclaimer

The Google Indexing API is officially documented for `JobPosting` and `BroadcastEvent` structured data only. Many SEO practitioners use it for general content and report that it works in practice. This system uses the URL Inspection API as the source of truth for index status, so even if the Indexing API stops working for general content, the system will continue to function as a monitoring tool.

## License

MIT
