# Architecture Decision Records (ADRs)

## ADR-001: SQLite with WAL Mode

### Status: Accepted

### Context

The system requires persistent storage for websites, URLs, and index status. We evaluated several database options:

- **PostgreSQL/MySQL**: Feature-rich but require separate server/process
- **SQLite**: Zero-config, single file, but traditionally has concurrency limitations
- **Embedded databases**: LevelDB, RocksDB - not ideal for relational data

### Decision

Use SQLite with WAL (Write-Ahead Logging) mode enabled.

### Rationale

1. **Zero-configuration deployment**: No database server to install or manage
2. **Single-file portability**: Easy backup, migration, and development
3. **WAL mode benefits**:
   - Concurrent reads during writes (reader doesn't block writer)
   - Better crash recovery
   - Improved write performance with async durability
4. **Adequate for single-user tool**: The application is designed for a single operator
5. **Strong Python support**: SQLAlchemy + aiosqlite provides excellent async support

### Consequences

**Positive:**
- Simple deployment (single file + Python)
- No external dependencies
- Fast reads for our query patterns

**Negative:**
- Limited concurrency for write-heavy workloads
- Not suitable for multi-instance deployments
- Requires careful connection pooling

### Implementation Notes

```python
# WAL mode is enabled via PRAGMA
cursor.execute("PRAGMA journal_mode=WAL;")

# Foreign keys enabled for data integrity
cursor.execute("PRAGMA foreign_keys=ON;")

# Busy timeout to handle transient locks
connect_args={"timeout": 30}
```

---

## ADR-002: HTMX over Single-Page Application

### Status: Accepted

### Context

The system needs a web interface for managing websites, viewing queue status, and adjusting priorities. We evaluated frontend approaches:

- **Single-Page Application (React/Vue)**: Rich interactivity but complex build pipeline
- **Server-Side Rendering (Django/Rails)**: Simple but full page reloads
- **HTMX**: Server-rendered with progressive enhancement

### Decision

Use HTMX with server-side Jinja2 templates.

### Rationale

1. **Minimal JavaScript**: Avoid complex frontend build pipeline
2. **Fast development**: Templates and backend in same language (Python)
3. **Progressive enhancement**: Works without JS, enhanced with HTMX
4. **No API surface to maintain**: Direct template rendering, no REST contract
5. **Lightweight deployment**: Single application binary/process

### Consequences

**Positive:**
- Single deployment artifact
- Simpler development workflow
- Excellent for CRUD-style interfaces
- Small attack surface (no client-side API)

**Negative:**
- More server processing per request
- Less rich interactivity than SPA
- Not ideal for real-time dashboards

### Implementation Notes

```html
<!-- Example HTMX partial update -->
<button hx-post="/queue/reprioritize"
        hx-vals='{"url_id": "{{ url.id }}", "priority": "high"}'
        hx-swap="outerHTML">
    Boost Priority
</button>
```

---

## ADR-003: APScheduler for Background Jobs

### Status: Accepted

### Context

The system needs to run periodic tasks for:
- URL submission to Google Indexing API
- Index verification via URL Inspection API
- Sitemap refreshing

We evaluated:
- **System cron**: External dependency, no overlap protection
- **Celery + Redis**: Powerful but complex for this use case
- **APScheduler**: In-process scheduler with Python-native API
- **Temporal**: Overkill for our requirements

### Decision

Use APScheduler with AsyncIOScheduler.

### Rationale

1. **No external dependencies**: Runs in-process with the application
2. **Python-native**: Excellent async support via AsyncIOScheduler
3. **Job persistence**: Built-in SQLAlchemy job store for durability
4. **Overlap protection**: Can configure to prevent concurrent execution
5. **Flexible scheduling**: Interval and cron triggers supported

### Consequences

**Positive:**
- Simple deployment (no Redis/worker processes)
- Jobs restart with application
- Configurable overlap protection
- Good logging and event hooks

**Negative:**
- Single-point-of-failure (if app crashes, jobs stop)
- Not suitable for horizontally scaled deployments
- Job state in-memory unless using job store

### Implementation Notes

```python
scheduler = AsyncIOScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=jobstore_url)}
)

scheduler.add_job(
    func=run_scheduled_url_submission_job,
    trigger="interval",
    seconds=300,
    id=URL_SUBMISSION_JOB_ID,
)
```

---

## ADR-004: Dual-API Strategy (Indexing + URL Inspection)

### Status: Accepted

### Context

Google provides two relevant APIs:
1. **Indexing API**: Submit URL update/delete notifications
2. **URL Inspection API**: Check actual index status

We needed to decide how to use these APIs for the submission/verification workflow.

### Decision

Use both APIs - Indexing API for submissions, URL Inspection API for verification.

### Rationale

1. **Indexing API limitations**: The metadata endpoint only confirms Google *received* your notification, not whether the URL is actually indexed
2. **URL Inspection provides ground truth**: Returns coverageState (e.g., "Submitted and indexed", "Crawled - currently not indexed")
3. **Graceful degradation**: If Indexing API stops working for general content, system continues to verify status
4. **Industry practice**: SEO tools commonly use both APIs

### Consequences

**Positive:**
- Complete visibility: submission + verification
- Ground truth index status, not just notification status
- Resilient to API changes

**Negative:**
- Two API quotas to manage
- More complex client setup
- Higher API usage

### API Quotas

| API | Default Quota | Per |
|:---|:---|:---|
| Indexing API (publish) | 200/day | Project |
| Indexing API (metadata) | 180/min | Project |
| URL Inspection | 2,000/day | Property |

---

## ADR-005: Per-Website Rate Limiting with Quota Isolation

### Status: Accepted

### Context

The system supports multiple websites, each with its own Google service account and quota. We needed a rate limiting strategy that:

1. Enforces per-website quotas
2. Prevents one website from consuming another website's quota
3. Handles quota resets gracefully

### Decision

Implement per-website rate limiting using in-memory quota tracking with datetime-based sliding window.

### Rationale

1. **Quota isolation**: Each website uses its own service account credentials
2. **Independent limits**: One site's quota exhaustion doesn't affect others
3. **Simple implementation**: In-memory tracking avoids external dependencies
4. **Sliding window**: Respects daily quotas with automatic reset

### Implementation Notes

```python
class RateLimiterService:
    def __init__(self, quota_service: QuotaService):
        self._quotas: dict[str, QuotaState] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    async def acquire(self, website_id: UUID, api_type: str) -> Permit:
        # Check daily quota, reset if new day
        # Use semaphore for concurrency control
        # Return permit that must be released after API call
```

### Consequences

**Positive:**
- Complete quota isolation between websites
- Simple implementation
- No external rate limiting service needed

**Negative:**
- Quota state lost on restart (acceptable - quotas reset daily)
- Single-instance only (not shared across instances)

---

## ADR-006: Priority Queue with Auto-Calculation

### Status: Accepted

### Context

URLs discovered from sitemaps need to be submitted to Google's Indexing API in an optimal order. We needed a priority system that:

1. Prioritizes recently modified URLs
2. Allows manual override for urgent URLs
3. Handles URLs without `lastmod` gracefully

### Decision

Implement a two-tier priority system:
1. Manual priority (1-100) - user-set override
2. Auto-calculated priority based on `lastmod` age

### Rationale

1. **SEO value**: Recently modified content typically needs faster indexing
2. **Manual control**: Users may need to prioritize specific URLs
3. **Graceful degradation**: URLs without `lastmod` still get processed (lowest priority)

### Priority Calculation Logic

```python
def calculate_auto_priority(lastmod: datetime | None) -> int:
    if lastmod is None:
        return 10  # Lowest priority

    days_old = (now - lastmod).days

    if days_old <= 1:
        return 100
    elif days_old <= 7:
        return 80 + (7 - days_old)  # 80-99
    elif days_old <= 30:
        return 50 + (30 - days_old) // 7 * 10  # 50-70
    else:
        return max(10, 50 - (days_old - 30) // 30 * 10)  # 10-40
```

### Consequences

**Positive:**
- Recently modified URLs indexed first
- Manual override for urgent submissions
- All URLs eventually processed

**Negative:**
- Complexity in priority calculation
- Need to track manual vs auto priority separately

---

## ADR-007: Batch Processing with Overlap Protection

### Status: Accepted

### Context

Scheduled jobs run periodically to process URLs. We needed to ensure:
1. Jobs don't run concurrently (overlap protection)
2. Long-running jobs don't block the next scheduled run
3. Job metrics are tracked for monitoring

### Decision

Implement overlap protection using asyncio locks with metrics tracking.

### Rationale

1. **Prevent resource exhaustion**: Concurrent API calls could exhaust quotas
2. **Predictable behavior**: Skip if already running vs queue
3. **Monitoring**: Track runs, successes, failures, duration

### Implementation Notes

```python
class _OverlapProtectedRunner:
    async def run(self, job_id: str, run: Callable) -> None:
        lock = self._locks[job_id]
        if lock.locked():
            self._metrics[job_id].overlap_skips += 1
            return  # Skip this run

        async with lock:
            # Execute job with metrics tracking
            await run()
```

### Consequences

**Positive:**
- No concurrent job execution
- Clear metrics for monitoring
- Graceful skip on overlap

**Negative:**
- Missed runs if job takes longer than interval
- Need to tune interval based on job duration
