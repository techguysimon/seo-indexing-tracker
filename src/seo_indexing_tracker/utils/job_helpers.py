from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from seo_indexing_tracker.services.google_url_inspection_client import IndexStatusResult
from seo_indexing_tracker.utils.shared_helpers import (
    extract_index_status_result,
    optional_text,
    parse_verdict,
)


def is_transient_quota_error_code(error_code: str | None) -> bool:
    return error_code in {"RATE_LIMITED", "QUOTA_EXCEEDED"}


def build_index_status_row(
    *,
    url_id: UUID,
    result: IndexStatusResult,
    index_status_result: dict[str, Any] | None = None,
) -> dict[str, object]:
    if index_status_result is None:
        index_status_result = {}
        raw_response = result.raw_response or {}
        extracted = extract_index_status_result(raw_response)
        index_status_result = extracted

    return {
        "url_id": url_id,
        "coverage_state": result.coverage_state or "INSPECTION_FAILED",
        "verdict": parse_verdict(result.verdict),
        "last_crawl_time": result.last_crawl_time,
        "indexed_at": result.last_crawl_time,
        "checked_at": datetime.now(UTC),
        "robots_txt_state": result.robots_txt_state,
        "indexing_state": result.indexing_state,
        "page_fetch_state": optional_text(index_status_result.get("pageFetchState")),
        "google_canonical": optional_text(index_status_result.get("googleCanonical")),
        "user_canonical": optional_text(index_status_result.get("userCanonical")),
        "raw_response": result.raw_response
        or {
            "error_code": result.error_code,
            "error_message": result.error_message,
        },
    }
