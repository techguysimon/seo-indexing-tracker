# Architecture Documentation

## High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                      SEO Indexing Tracker                             │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌────────────────┐      ┌────────────────┐     ┌───────────────┐   │
│  │  Website &     │─────▶│  Sitemap       │────▶│  URL Queue    │   │
│  │  Config Mgr    │      │  Fetcher       │     │  Manager      │   │
│  └────────────────┘      └────────────────┘     └───────────────┘   │
│         │                                              │            │
│         ▼                                              ▼            │
│  ┌────────────────┐      ┌────────────────┐     ┌───────────────┐   │
│  │  Service Acct │─────▶│  Quota &       │────▶│  Scheduler    │   │
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
│  │   Dashboard)  │           │  • Indexing API v3           │      │
│  └────────────────┘           │  • Search Console API v1     │      │
│         │                     └──────────────────────────────┘      │
│         ▼                                                           │
│  ┌────────────────┐                                                 │
│  │  SQLite DB    │                                                 │
│  │  (WAL mode)   │                                                 │
│  └────────────────┘                                                 │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

## Component Overview

### API Layer (`src/seo_indexing_tracker/api/`)

The API layer handles HTTP requests and responses. Each module corresponds to a domain:

| Module | Purpose |
|:---|:---|
| `websites.py` | CRUD operations for website configurations |
| `sitemaps.py` | Sitemap registration and management |
| `service_accounts.py` | Google service account credential management |
| `queue.py` | Priority queue operations (enqueue, dequeue, reprioritize) |
| `scheduler.py` | Scheduler job control (pause, resume, view status) |
| `web.py` | Web UI routes serving HTMX templates |
| `config_validation.py` | Settings validation endpoints |

### Service Layer (`src/seo_indexing_tracker/services/`)

Business logic resides here:

| Service | Responsibility |
|:---|:---|
| `scheduler.py` | APScheduler lifecycle management, job registration |
| `processing_pipeline.py` | Scheduled job execution with overlap protection |
| `priority_queue.py` | URL priority management and dequeue logic |
| `url_discovery.py` | Sitemap fetching, parsing, URL extraction |
| `rate_limiter.py` | Per-website API quota enforcement |
| `google_api_factory.py` | Factory for creating Google API clients per website |
| `google_url_inspection_client.py` | URL Inspection API integration |
| `batch_processor.py` | Batch URL submission to Indexing API |

### Model Layer (`src/seo_indexing_tracker/models/`)

SQLAlchemy ORM models defining database schema:

- **Website** - Website configuration (domain, site_url, active status)
- **ServiceAccount** - Google service account credentials per website
- **Sitemap** - Sitemap URL sources linked to websites
- **URL** - Discovered URLs with metadata (lastmod, priority, status)
- **IndexStatus** - Index verification results from URL Inspection API
- **SubmissionLog** - Indexing API submission history

### Schema Layer (`src/seo_indexing_tracker/schemas/`)

Pydantic models for request/response validation and serialization.

## Data Flow for Key Operations

### 1. URL Discovery Flow

```
Sitemap URL
    │
    ▼
┌─────────────────┐
│ Sitemap Fetcher │  Fetch XML via HTTP (gzip + content-encoding hardening)
└────────┬────────┘
         │
         ▼
┌─────────────────────┐
│ Sitemap Type Detector│  Detect index vs urlset
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ URL Parser          │  Extract URLs with lastmod, changefreq
│ + recurse child maps│
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐     ┌─────────────────┐
│ URL Discovery Service│────▶│ Database        │
│ (detect new/changed)│     │ (store URLs)   │
└─────────────────────┘     └─────────────────┘
           │
           ▼
┌─────────────────────┐
│ Priority Queue      │  Auto-calculate priority
│ (enqueue new URLs) │  (recently modified = higher)
└─────────────────────┘
```

### Sitemap Child Traversal Security Model

When a sitemap is a `<sitemapindex>`, child sitemap traversal is guarded by strict policy checks:

1. **Child URL policy validation**: only `http`/`https`, hostname required, and resolved IPs must not be private/loopback/link-local/reserved/multicast/unspecified.
2. **Explicit redirect handling**: redirects are followed manually (not automatically), with per-hop policy validation and hop limits.
3. **Pinned connect destination**: fetches pin to validated connect IPs and can retry on validated fallback IPs for transient network failures.
4. **Fail-closed destination checks**: if peer connect metadata is unavailable or disallowed, traversal fails instead of continuing.

### Trigger Indexing Diagnostics

Manual trigger indexing reports and logs failures by stage so operators can act quickly:

- `fetch` for sitemap retrieval/HTTP problems
- discovery processing stages such as `parse`, `index_depth_limit`, `index_child_limit`, `fetch_child_policy`, `fetch_child`
- `enqueue` when queue persistence fails after discovery

Structured logs use `sitemap_url_sanitized` (host/path only) for safer diagnostics.

### 2. Indexing Submission Flow

```
Scheduler Trigger
      │
      ▼
┌──────────────────────────────┐
│ URL Submission Job           │
│ (SchedulerProcessingPipeline)│
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Priority Queue               │  Dequeue highest priority URLs
│ (dequeue_batched)            │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Rate Limiter                 │  Check per-website quota
│ (acquire permit)             │  Wait if limit reached
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Google Indexing API         │  Submit URL_UPDATED notification
│ (batch_submit)               │  Up to 100 URLs per batch
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Submission Log              │  Record success/failure
│ (database)                  │
└──────────────────────────────┘
```

### 3. Index Verification Flow

```
Scheduler Trigger
      │
      ▼
┌──────────────────────────────┐
│ Index Verification Job       │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Select URLs for Verification │
│ (oldest checked first)       │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Rate Limiter                 │  Check inspection API quota
│ (acquire permit)             │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ URL Inspection API           │  Get coverageState, indexingState
│ (inspect_url)               │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Index Status Record          │  Map to system status
│ (database)                  │  INDEXED, NOT_INDEXED, BLOCKED, etc.
└──────────────────────────────┘
```

### 4. Sitemap Refresh Flow

```
Scheduler Trigger (hourly default)
      │
      ▼
┌──────────────────────────────┐
│ Sitemap Refresh Job          │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Active Sitemaps Query         │
└──────────────┬───────────────┘
               │
        ┌──────┴──────┐
        │ For each   │
        │ sitemap    │
        └──────┬──────┘
               │
               ▼
┌──────────────────────────────┐
│ URL Discovery Service        │  Re-parse sitemap
│ (discover_urls)              │  Compare lastmod timestamps
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Detect Changes               │
│ • New URLs (not in DB)       │
│ • Modified URLs (newer       │
│   lastmod than DB record)    │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Update Database             │
│ • Insert new URLs           │
│ • Update modified URLs      │
│ • Mark unchanged URLs       │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Requeue Changed URLs        │  Enqueue for submission
│ (priority_queue.enqueue_many)│
└──────────────────────────────┘
```

## Queue System

### Priority Calculation

Priority is calculated automatically based on URL metadata:

1. **Manual Override**: User-set priority takes precedence (1-100)
2. **Auto-calculated**: If no manual priority, calculate from `lastmod`:
   - Recently modified URLs (within 7 days): Priority 80-100
   - Older URLs: Priority decreases with age
   - No `lastmod`: Priority 10 (lowest auto priority)

### Dequeue Strategy

The system uses a batched dequeue approach:

1. Query URLs with `current_priority > 0` ordered by priority DESC, then `updated_at` ASC
2. Dequeue configurable batch size (default: 100)
3. Mark dequeued URLs with `last_attempted_at` timestamp
4. Failed URLs remain in queue for retry

### Rate Limiting

Rate limiting is enforced per website:

- **Indexing API**: Configurable daily limit (default: 200/day)
- **URL Inspection API**: Configurable daily limit (default: 2000/day)

The `RateLimiterService` uses:
- In-memory quota tracking with datetime-based reset
- Semaphore-based concurrency limiting
- Configurable per-website limits via settings

## Scheduler Jobs

The system runs three background jobs via APScheduler:

| Job | Interval | Purpose |
|:---|:---|:---|
| URL Submission Job | 5 min (configurable) | Dequeue and submit URLs to Indexing API |
| Index Verification Job | 15 min (configurable) | Verify index status via URL Inspection API |
| Sitemap Refresh Job | 60 min (configurable) | Re-parse sitemaps, discover new/changed URLs |

### Job Features

- **Overlap Protection**: Jobs skip execution if already running
- **Metrics Tracking**: Each job tracks runs, successes, failures, duration
- **Event Logging**: All job events logged with structured logging
- **Database Job Store**: APScheduler jobs persisted in SQLite

### Job Execution Flow

```
APScheduler Trigger
        │
        ▼
┌─────────────────────┐
│ OverlapProtectedRunner│
│ (check if running) │
└──────────┬──────────┘
           │
    ┌──────┴──────┐
    │ If locked: │
    │ skip with  │
    │ metrics    │
    └──────┬──────┘
           │
           ▼
┌─────────────────────┐
│ Execute Job Logic   │
│ (track start time)  │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ Record Metrics      │
│ • success/failure  │
│ • duration         │
│ • error message    │
└─────────────────────┘
```

## Database Schema

The database uses SQLite with WAL (Write-Ahead Logging) mode for:

- Concurrent read access during writes
- Crash recovery
- Better performance for read-heavy workloads

### Key Tables

- **websites**: Website configurations
- **service_accounts**: Per-website Google credentials
- **sitemaps**: Sitemap sources linked to websites
- **urls**: Discovered URLs with metadata
- **index_statuses**: Index verification results
- **submission_logs**: API submission history

## Web UI

The frontend uses HTMX for dynamic interactions without JavaScript:

- Server-rendered HTML via Jinja2 templates
- HTMX for progressive enhancement (partial page updates)
- Minimal client-side JavaScript
- Responsive design with CSS

### Key Pages

- **Dashboard**: Overview with queue stats, recent activity
- **Websites**: List, add, edit website configurations
- **Website Detail**: End-to-end setup workflow (service account, sitemap management, trigger indexing, delete actions)
- **Queue**: View and manage URL queue, adjust priorities, and run batch actions via HTMX partial updates
- **Jobs**: Scheduler status and metrics
