# Code Review Plan - SEO Indexing Tracker

## Completed Work

### Phase 1: Quick Wins ✅
- **Closure bug fix** in `main.py` - Signal handlers now correctly capture loop variable
- **Created `utils/shared_helpers.py`** - Consolidated duplicated utility functions
- **Fixed hardcoded 2026 test dates** - Tests now use realistic/relative dates
- **Removed no-op validator** in `config.py`

### Phase 2: web.py Reduction ✅
- **Extracted `services/url_inspection_service.py`** - URL inspection logic moved from web.py
- **Extracted `services/url_submission_service.py`** - URL submission logic moved from web.py
- **Extracted `services/trigger_indexing_service.py`** - Trigger indexing logic moved from web.py
- **Created `utils/form_helpers.py`** - Form handling helpers extracted from web.py
- **Line count: 2013 → ~1700** (reduced by ~300 lines)

### Phase 3: Service Refactoring (Partial) ✅
- **Extracted `services/cooldown_service.py`** - Cooldown management from processing_pipeline.py
- **Line count: 1211 → 1098** (reduced by ~110 lines)

---

## CRITICAL Issues (Must Address)

### 1. God Objects - SRP Violations

| File | Lines | Issue |
|------|-------|-------|
| `api/web.py` | **~1700** | Still too many concerns: dashboard, queue management, website CRUD |
| `services/processing_pipeline.py` | **1098** | Job scheduling, 3 job types, rate limiting, checkpointing |
| `services/batch_processor.py` | **1281** | Batch dequeue, inspection, submission, progress, requeue logic |

### 2. Business Logic in Routes
| File | Business Logic in Route |
|------|------------------------|
| `queue.py` | Queue stats, batch ops, triggers |
| `quota.py` | `set_quota_override()` logic |
| `web.py` | Google API calls, trigger indexing, form handling |
| `activity.py` | Direct SQL queries |
| `sitemap_progress.py` | Database queries |
| `urls.py` | CSV export, query building |

### 3. Dependency Inversion Violations
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

### Phase 1: Quick Wins (No Breaking Changes) ✅ DONE
1. ~~Fix closure bug in `main.py`~~
2. ~~Create `utils/shared_helpers.py` with duplicated functions~~
3. ~~Fix hardcoded 2026 test dates~~
4. ~~Remove no-op validator in `config.py`~~

### Phase 2: Reduce web.py from 2013 lines ✅ DONE
1. ~~Extract `services/trigger_indexing_service.py`~~
2. ~~Extract `services/url_inspection_service.py`~~
3. ~~Extract `services/url_submission_service.py`~~
4. ~~Move form handling to `utils/form_helpers.py`~~
5. Split `api/web.py` into smaller modules
6. Move remaining business logic from routes to services

### Phase 3: Service Refactoring (Partial ✅, In Progress)
1. ~~Extract `services/cooldown_service.py`~~
2. Split `processing_pipeline.py` into job classes
3. Split `batch_processor.py` into specialized services
4. Add proper DI for settings and session_factory

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
- Extracted shared helpers to reduce duplication

(End of file - total 97 lines)
