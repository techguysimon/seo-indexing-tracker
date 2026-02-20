# SEO Indexing Tracker System Design — v2.0

## Executive Summary

This document provides a comprehensive design for an **SEO Indexing Tracker** system that monitors, manages, and optimizes URL indexing status across Google Search. The system uses a **dual-API strategy**: the Google Indexing API for URL submission notifications, and the Google Search Console URL Inspection API for actual index status verification. It supports **multiple websites**, each with independent sitemap configurations and service accounts for quota isolation.

**Key Features:**
- Multi-website support with per-site service accounts and quota isolation
- Multiple sitemap sources per website (sitemap index files + standalone sitemaps)
- Automatic sitemap parsing with `<lastmod>` change detection
- Google Indexing API integration for URL submission (with risk acknowledgment)
- Google Search Console URL Inspection API for true index status verification
- Intelligent priority queue with manual override support
- Web UI dashboard with queue management, stats, and priority reshuffling
- SQLite-backed persistence for lightweight, zero-config deployment
- Crash-safe rate limiting and scheduling with overlap protection

**Important API Disclaimer:**
The Google Indexing API is officially documented for `JobPosting` and `BroadcastEvent` structured data only. Many SEO practitioners use it for general content and report that it works in practice — Google has confirmed it won't cause negative impact — but Google's Gary Illyes has warned it could "stop working for unsupported verticals overnight." This system is designed to work regardless: the Indexing API handles submission *notifications*, while the URL Inspection API provides ground-truth index verification. If the Indexing API stops working for general content, the system degrades gracefully to a monitoring-only tool, and sitemap-based discovery remains the fallback submission path.

---

## 1. Architecture Overview

### 1.1 System Components

```
┌──────────────────────────────────────────────────────────────────────┐
│                      SEO Indexing Tracker                            │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌────────────────┐      ┌────────────────┐     ┌───────────────┐   │
│  │  Website &     │─────▶│  Sitemap       │────▶│  URL Queue    │   │
│  │  Config Mgr    │      │  Fetcher       │     │  Manager      │   │
│  └────────────────┘      └────────────────┘     └───────────────┘   │
│         │                                              │            │
│         ▼                                              ▼            │
│  ┌────────────────┐      ┌────────────────┐     ┌───────────────┐   │
│  │  Service Acct  │─────▶│  Quota &       │────▶│  Scheduler    │   │
│  │  Manager       │      │  Rate Limiter  │     │  & Priority   │   │
│  └────────────────┘      └────────────────┘     └───────────────┘   │
│                                                    │       │        │
│                                              ┌─────┘       └─────┐  │
│                                              ▼                   ▼  │
│                                  ┌──────────────────┐ ┌──────────┐  │
│                                  │  Indexing API     │ │ URL      │  │
│                                  │  Client           │ │ Inspect  │  │
│                                  │  (Submission)     │ │ Client   │  │
│                                  └──────────────────┘ └──────────┘  │
│                                         │                   │       │
│  ┌────────────────┐                     ▼                   ▼       │
│  │  Web UI        │◀──────▶   ┌──────────────────────────────┐      │
│  │  (FastAPI +    │           │  Google APIs                 │      │
│  │   Dashboard)   │           │  • Indexing API v3           │      │
│  └────────────────┘           │  • Search Console API v1     │      │
│         │                     └──────────────────────────────┘      │
│         ▼                                                           │
│  ┌────────────────┐                                                 │
│  │  SQLite DB     │                                                 │
│  └────────────────┘                                                 │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### 1.2 Data Flow

1. **Website Configuration**: Register websites with domain, service account, and sitemap URLs
2. **Sitemap Ingestion**: Fetch and parse XML sitemaps (index files + standalone), extract URLs with `<lastmod>`
3. **URL Discovery & Deduplication**: Store URLs in database, detect new/changed URLs via `lastmod` comparison
4. **Priority Assignment**: Auto-calculate priority; respect manual overrides
5. **Rate-Limited Submission**: Submit URLs to Google Indexing API respecting per-website quotas
6. **Index Verification**: Check actual index status via Search Console URL Inspection API
7. **Status Tracking**: Record full lifecycle with timestamps, API responses, and error states
8. **UI Dashboard**: Display stats, queue, and allow priority management

### 1.3 Tech Stack

| Layer | Technology | Rationale |
|:---|:---|:---|
| **Backend** | Python 3.11+ / FastAPI | Async support, lightweight, excellent Google API client libraries |
| **Database** | SQLite (WAL mode) | Zero-config, single-file, fast reads, perfect for single-user tool |
| **Frontend** | HTMX + Jinja2 templates | Minimal JS, server-rendered, lightweight, fast to build |
| **Scheduling** | APScheduler (AsyncIOScheduler) | In-process, no external dependencies, supports cron triggers |
| **API Clients** | `google-api-python-client`, `google-auth` | Official Google libraries |
| **Sitemap Parsing** | `lxml` with iterparse | Streaming XML parsing for memory efficiency |
| **Deployment** | Docker Compose with auto-restart | Reliable, reproducible, easy backup (mount SQLite volume) |

---

## 2. API Integration

### 2.1 Dual-API Strategy

This system uses **two separate Google APIs** for different purposes:

| API | Purpose | Quota | Auth Scope |
|:---|:---|:---|:---|
| **Google Indexing API v3** | Submit URL notifications (crawl requests) | 200 publish/day per project | `https://www.googleapis.com/auth/indexing` |
| **Search Console URL Inspection API** | Verify actual index status | 2,000/day per site property | `https://www.googleapis.com/auth/webmasters` |

**Why two APIs?**
- The Indexing API's metadata endpoint only confirms Google *received* your notification — not whether the URL is actually indexed
- The URL Inspection API returns ground-truth: `coverageState`, `robotsTxtState`, `indexingState`, `lastCrawlTime`, `googleCanonical`, etc.

### 2.2 Google Indexing API v3 (Submission)

**Base URL**: `https://indexing.googleapis.com/v3`

#### 2.2.1 Publish Endpoint

```
POST /urlNotifications:publish
Content-Type: application/json
Authorization: Bearer {access_token}

{
  "url": "https://example.com/page",
  "type": "URL_UPDATED"  // or "URL_DELETED"
}
```

**Response:**
```json
{
  "url": "https://example.com/page",
  "type": "URL_UPDATED",
  "notifyTime": "2026-02-19T20:25:00Z"
}
```

#### 2.2.2 Batch Endpoint

```
POST /batch
Content-Type: multipart/mixed; boundary="===============xxx=="

// Combines up to 100 individual publish requests in a single HTTP call
// Each URL still counts individually against the daily quota
```

#### 2.2.3 Metadata Endpoint (Notification Status Only)

```
GET /urlNotifications/metadata?url={encoded_url}
Authorization: Bearer {access_token}
```

Returns when Google last received your notification — **not** actual index status.

#### 2.2.4 Quotas (Indexing API)

| Quota | Default | Reset |
|:---|:---|:---|
| `DefaultPublishRequestsPerDayPerProject` | 200/day | Midnight Pacific Time |
| `DefaultMetadataRequestsPerMinutePerProject` | 180/min | Rolling |
| `DefaultRequestsPerMinutePerProject` | 380/min | Rolling |

### 2.3 Google Search Console URL Inspection API (Verification)

**Base URL**: `https://searchconsole.googleapis.com/v1`

#### 2.3.1 Inspect Endpoint

```
POST /urlInspection/index:inspect
Content-Type: application/json
Authorization: Bearer {access_token}

{
  "inspectionUrl": "https://example.com/page",
  "siteUrl": "sc-domain:example.com",
  "languageCode": "en"
}
```

**Response (key fields):**
```json
{
  "inspectionResult": {
    "indexStatusResult": {
      "verdict": "PASS",
      "coverageState": "Submitted and indexed",
      "robotsTxtState": "ALLOWED",
      "indexingState": "INDEXING_ALLOWED",
      "lastCrawlTime": "2026-02-15T08:30:00Z",
      "pageFetchState": "SUCCESSFUL",
      "googleCanonical": "https://example.com/page",
      "userCanonical": "https://example.com/page",
      "crawledAs": "DESKTOP"
    },
    "mobileUsabilityResult": {
      "verdict": "PASS"
    }
  }
}
```

**Key `coverageState` values for status mapping:**

| coverageState | Maps to System Status |
|:---|:---|
| `Submitted and indexed` | `INDEXED` |
| `Indexed, not submitted in sitemap` | `INDEXED` |
| `Crawled - currently not indexed` | `NOT_INDEXED` |
| `Discovered - currently not indexed` | `NOT_INDEXED` |
| `Page with redirect` | `REDIRECTED` |
| `URL is unknown to Google` | `UNKNOWN` |
| `Blocked by robots.txt` | `BLOCKED` |
| `Blocked by noindex` | `BLOCKED` |
| `Duplicate without user-selected canonical` | `DUPLICATE` |

#### 2.3.2 Quotas (URL Inspection API)

| Quota | Limit | Scope |
|:---|:---|:---|
| Per-site | 2,000 QPD, 600 QPM | Per Search Console property |
| Per-project | 10,000,000 QPD, 15,000 QPM | Per GCP project |

### 2.4 Authentication

Each website has its own service account for quota isolation:

```python
from google.oauth2 import service_account
from googleapiclient.discovery import build

class GoogleAPIClient:
    """Per-website API client with isolated credentials"""

    INDEXING_SCOPES = ['https://www.googleapis.com/auth/indexing']
    WEBMASTER_SCOPES = [
        'https://www.googleapis.com/auth/webmasters',
        'https://www.googleapis.com/auth/webmasters.readonly'
    ]

    def __init__(self, service_account_path: str):
        self.service_account_path = service_account_path
        self._indexing_service = None
        self._search_console_service = None

    @property
    def indexing_service(self):
        if self._indexing_service is None:
            creds = service_account.Credentials.from_service_account_file(
                self.service_account_path,
                scopes=self.INDEXING_SCOPES
            )
            self._indexing_service = build('indexing', 'v3', credentials=creds)
        return self._indexing_service

    @property
    def search_console_service(self):
        if self._search_console_service is None:
            creds = service_account.Credentials.from_service_account_file(
                self.service_account_path,
                scopes=self.WEBMASTER_SCOPES
            )
            self._search_console_service = build(
                'searchconsole', 'v1', credentials=creds
            )
        return self._search_console_service

    async def submit_url(self, url: str, action: str = 'URL_UPDATED') -> dict:
        """Submit URL to Indexing API"""
        return self.indexing_service.urlNotifications().publish(
            body={'url': url, 'type': action}
        ).execute()

    async def inspect_url(self, url: str, site_url: str) -> dict:
        """Check index status via URL Inspection API"""
        return self.search_console_service.urlInspection().index().inspect(
            body={
                'inspectionUrl': url,
                'siteUrl': site_url,
                'languageCode': 'en'
            }
        ).execute()

    async def batch_submit(self, urls: list[str], action: str = 'URL_UPDATED'):
        """Batch submit up to 100 URLs"""
        batch = self.indexing_service.new_batch_http_request()
        results = {}

        def callback(request_id, response, exception):
            results[request_id] = {
                'response': response,
                'error': str(exception) if exception else None
            }

        for i, url in enumerate(urls[:100]):
            batch.add(
                self.indexing_service.urlNotifications().publish(
                    body={'url': url, 'type': action}
                ),
                request_id=str(i),
                callback=callback
            )

        batch.execute()
        return results
```

**Setup requirements per website:**
1. Create a GCP project (or reuse one — but separate projects = separate Indexing API quotas)
2. Enable both **Indexing API** and **Search Console API** in the project
3. Create a service account, download JSON key
4. Add the service account email as **Owner** in Google Search Console for the property
5. Add the service account email as **Owner** in Webmaster Central for Indexing API access

---

## 3. Sitemap Parsing

### 3.1 Multi-Sitemap Architecture

Each website can have multiple sitemap sources, each of which may be:
- A **sitemap index** (`<sitemapindex>`) containing references to child sitemaps
- A **standalone sitemap** (`<urlset>`) containing URLs directly

```python
from lxml import etree
from datetime import datetime
from dataclasses import dataclass
import gzip
import httpx

SITEMAP_NS = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}


@dataclass
class DiscoveredURL:
    url: str
    lastmod: datetime | None
    changefreq: str | None
    sitemap_priority: str | None
    sitemap_source: str
    discovered_at: datetime


async def fetch_sitemap(url: str, client: httpx.AsyncClient) -> tuple[bytes, str | None, str | None]:
    """Fetch sitemap with gzip support and ETag/Last-Modified caching"""
    response = await client.get(url, follow_redirects=True)
    response.raise_for_status()

    content = response.content

    # Handle gzipped sitemaps (.xml.gz)
    if url.endswith('.gz') or response.headers.get('content-encoding') == 'gzip':
        content = gzip.decompress(content)

    return content, response.headers.get('etag'), response.headers.get('last-modified')


def detect_sitemap_type(content: bytes) -> str:
    """Detect whether content is a sitemap index or a urlset"""
    root = etree.fromstring(content)
