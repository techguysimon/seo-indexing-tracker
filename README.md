# SEO Indexing Tracker

A system to monitor, manage, and optimize URL indexing status across Google Search.

## Features

- Multi-website support with per-site service accounts and quota isolation
- Multiple sitemap sources per website (sitemap index files + standalone sitemaps)
- Automatic sitemap parsing with lastmod change detection
- Google Indexing API integration for URL submission
- Google Search Console URL Inspection API for index status verification
- Intelligent priority queue with manual override support
- Web UI dashboard with queue management, stats, and priority reshuffling
- SQLite-backed persistence for lightweight, zero-config deployment

## Tech Stack

- **Backend**: Python 3.11+ / FastAPI
- **Database**: SQLite (WAL mode)
- **Frontend**: HTMX + Jinja2 templates
- **Scheduling**: APScheduler (AsyncIOScheduler)

## Setup

1. Install dependencies:
   ```bash
   pip install -e ".[dev]"
   ```

2. Configure environment variables (see `.env.example`)

3. Run the development server:
   ```bash
   uvicorn src.main:app --reload
   ```

## Development

- **Build**: `pip install -e ".[dev]"`
- **Test**: `pytest`
- **Lint**: `ruff check .`
- **Format**: `ruff format .`
