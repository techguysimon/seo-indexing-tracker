from datetime import UTC, datetime
from uuid import UUID

from seo_indexing_tracker.services.google_url_inspection_client import IndexStatusResult
from seo_indexing_tracker.utils.shared_helpers import parse_verdict


def is_transient_quota_error_code(error_code: str | None) -> bool:
    return error_code in {"RATE_LIMITED", "QUOTA_EXCEEDED"}


def build_index_status_row(
    *, url_id: UUID, result: IndexStatusResult
) -> dict[str, object]:
    return {
        "url_id": url_id,
        "coverage_state": result.coverage_state or "INSPECTION_FAILED",
        "verdict": parse_verdict(result.verdict),
        "last_crawl_time": result.last_crawl_time,
        "indexed_at": result.last_crawl_time,
        "checked_at": datetime.now(UTC),
        "robots_txt_state": result.robots_txt_state,
        "indexing_state": result.indexing_state,
        "page_fetch_state": None,
        "google_canonical": None,
        "user_canonical": None,
        "raw_response": result.raw_response
        or {
            "error_code": result.error_code,
            "error_message": result.error_message,
        },
    }
