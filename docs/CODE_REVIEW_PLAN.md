# Code Review Plan - SEO Indexing Tracker

## Executive Summary

The codebase has solid foundational architecture but suffers from **3 critical God Object services**, **extensive business logic in API routes**, and **duplicated code**.

---

## CRITICAL Issues (Must Address)

### 1. God Objects - SRP Violations

| File | Lines | Issue |
|------|-------|-------|
| `api/web.py` | **2013** | Too many concerns: dashboard, queue management, website CRUD, URL inspection/submission |
| `services/processing_pipeline.py` | **1211** | Job scheduling, 3 job types, cooldown management, rate limiting, checkpointing |
| `services/batch_processor.py` | **1281** | Batch dequeue, inspection, submission, progress, requeue logic |

### 2. Closure Loop Variable Capture Bug (`main.py:200-210`)
```python
for handled_signal in (signal.SIGTERM, signal.SIGINT):
    def _signal_handler(signum: int, frame: object | None) -> None:
        previous_handler = previous_handlers[signal.Signals(signum)]  # BUG
```
All signal handlers will execute with last `handled_signal` value.

### 3. Business Logic in Routes
| File | Business Logic in Route |
|------|------------------------|
| `queue.py` | Queue stats, batch ops, triggers |
| `quota.py` | `set_quota_override()` logic |
| `web.py` | Google API calls, trigger indexing, form handling |
| `activity.py` | Direct SQL queries |
| `sitemap_progress.py` | Database queries |
| `urls.py` | CSV export, query building |

### 4. Dependency Inversion Violations
Routes directly instantiate `WebsiteGoogleAPIClients` instead of using injected dependencies.

---

## HIGH Priority Issues

### DRY Violations - Duplicated Code

| Function | Locations | Status |
|----------|-----------|--------|
| `_index_status_row` | `batch_processor.py`, `processing_pipeline.py` | **DUPLICATED** |
| `_parse_verdict` / `_index_verdict` | `batch_processor.py`, `processing_pipeline.py`, `web.py` | **DUPLICATED** |
| `_extract_index_status_result` | `batch_processor.py`, `web.py` | **DUPLICATED** |
| `_normalize_tag_name` | 3 sitemap services | DUPLICATED |
| `_sanitize_sitemap_url` | `sitemap_fetcher`, `url_discovery` | DUPLICATED |
| URL validation | `google_indexing_client`, `google_url_inspection_client`, `sitemap_url_parser` | DUPLICATED |
| `quota.py:set_quota_override` | `quota.py`, `web.py` | DUPLICATED |

---

## Implementation Phases

### Phase 1: Quick Wins (No Breaking Changes)
1. Fix closure bug in `main.py`
2. Create `utils/shared_helpers.py` with duplicated functions
3. Fix hardcoded 2026 test dates
4. Remove no-op validator in `config.py`

### Phase 2: Reduce web.py from 2013 lines
1. Extract `services/trigger_service.py`
2. Extract `services/url_inspection_service.py`
3. Split `api/web.py` into smaller modules
4. Move business logic from routes to services

### Phase 3: Service Refactoring
1. Split `processing_pipeline.py` into job classes
2. Split `batch_processor.py` into specialized services
3. Add proper DI for settings and session_factory

### Phase 4: Polish
1. Simplify templates
2. Add missing tests
3. Remove dead code

---

## What Is Done Well
- Async/await throughout
- Service layer abstraction
- Rate limiting with per-website isolation
- Crash recovery with checkpointing
- SSRF protection in sitemap fetching
- WAL mode SQLite for crash safety
