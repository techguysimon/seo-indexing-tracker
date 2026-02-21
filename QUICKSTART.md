# Quick Start

Get up and running with SEO Indexing Tracker in minutes.

## Prerequisites

- Google Cloud Project with Indexing API + Search Console API enabled
- Service account JSON key file
- Service account added as Owner in Google Search Console
- Python dependencies installed with `uv sync --extra dev` (includes `python-multipart` required for UI form posts)

## Required Environment

Set at minimum:

```bash
DATABASE_URL=sqlite+aiosqlite:///./data/seo_indexing_tracker.db
SECRET_KEY=replace-with-a-long-random-secret
```

Optional but useful when validating/fetching sitemaps from stricter origins:

```bash
OUTBOUND_HTTP_USER_AGENT=BlueBeastBuildAgent
```

## Setup via Web UI (Recommended)

### Step 1: Create Website

1. Open `http://localhost:8000/ui/websites`
2. Add `domain` and `site_url`
3. Click the website row link to open its detail page

### Step 2: Configure Service Account (Website Detail Page)

On `http://localhost:8000/ui/websites/{website_id}`:

1. Enter service account name and JSON credentials path on disk
2. Keep `indexing` + `webmasters` scopes enabled unless you have a specific reason to change
3. Save and confirm it appears in the service account section

### Step 3: Add Sitemaps

On the same website detail page:

1. Add sitemap URL(s)
2. Select sitemap type (`URLSET` or `INDEX`)
3. Save and confirm each sitemap appears in the list

### Step 4: Trigger Initial Indexing

On the website detail page, click **Trigger indexing**.

This runs discovery and enqueue in one action and returns stage-aware feedback if something fails.

### Step 5: Manage Queue

Open `http://localhost:8000/ui/queue` to:

- filter queue rows
- set per-URL priority overrides
- run batch enqueue/recalculate/remove actions

### Step 6: Delete Actions (Cleanup)

Use delete buttons in UI for:

- website removal (websites list)
- service account removal (website detail)
- sitemap removal (website detail)

## API Setup (Alternative)

### Step 1: Create Website

```bash
curl -X POST http://localhost:8000/api/websites \
  -H "Content-Type: application/json" \
  -d '{"domain": "example.com", "site_url": "https://example.com"}'
```

Returns a website UUID.

### Step 2: Add Service Account

```bash
curl -X POST http://localhost:8000/api/websites/{website_id}/service-account \
  -H "Content-Type: application/json" \
  -d '{"name": "Main", "credentials_path": "/app/service-accounts/key.json", "scopes": ["indexing", "webmasters"]}'
```

Place your service account JSON key file on the server filesystem. Each website requires its own service account for quota isolation.

### Step 3: Add Sitemap

```bash
curl -X POST http://localhost:8000/api/websites/{website_id}/sitemaps \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/sitemap.xml"}'
```

The sitemap URL must be publicly reachable.

### Step 4: Trigger Initial Indexing

```bash
curl -X POST http://localhost:8000/api/queue/websites/{website_id}/trigger \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_SECRET_KEY" \
  -d '{"action": "enqueue_all"}'
```

## Or: Wait for Scheduled Jobs

If you prefer, scheduled jobs will handle everything automatically:

| Job | Interval |
|-----|----------|
| Sitemap Refresh | Every 1 hour |
| URL Submission | Every 5 minutes |
| Index Verification | Every 15 minutes |

## Key Notes

- Service account JSON file must be on server filesystem (not passed as content)
- Website domain and sitemap URLs must be publicly accessible
- Queue trigger endpoint requires `SECRET_KEY` authorization header
- Access the web UI at http://localhost:8000
- Sitemap fetcher retries 403 responses once with alternate browser-like headers
- Trigger indexing logs and UI errors are grouped by stage (`fetch`, discovery processing, `enqueue`)
- Sitemap child traversal (from sitemap indexes) enforces strict SSRF protections and fail-closed behavior
