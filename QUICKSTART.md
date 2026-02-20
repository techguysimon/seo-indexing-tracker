# Quick Start

Get up and running with SEO Indexing Tracker in minutes.

## Prerequisites

- Google Cloud Project with Indexing API + Search Console API enabled
- Service account JSON key file
- Service account added as Owner in Google Search Console

## Setup

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
