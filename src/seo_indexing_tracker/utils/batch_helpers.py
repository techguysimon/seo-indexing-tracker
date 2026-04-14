from collections.abc import Sequence

from seo_indexing_tracker.models import IndexStatus, SubmissionStatus
from seo_indexing_tracker.models.url import URLIndexStatus
from seo_indexing_tracker.services.google_indexing_client import IndexingURLResult
from seo_indexing_tracker.services.google_url_inspection_client import (
    IndexStatusResult,
)
from seo_indexing_tracker.utils.index_status import (
    derive_url_index_status_from_coverage_state,
)


def submission_status_from_result(result: IndexingURLResult) -> SubmissionStatus:
    if result.success:
        return SubmissionStatus.SUCCESS
    if result.error_code in {"QUOTA_EXCEEDED", "RATE_LIMITED"}:
        return SubmissionStatus.RATE_LIMITED
    return SubmissionStatus.FAILED


def inspection_shows_indexed(result: IndexStatusResult | None) -> bool:
    if result is None:
        return False
    coverage_state = result.coverage_state
    if not coverage_state:
        return False

    derived = derive_url_index_status_from_coverage_state(coverage_state)
    return derived == URLIndexStatus.INDEXED


def is_already_indexed(index_status: IndexStatus | None) -> bool:
    if index_status is None:
        return False

    coverage_state = index_status.coverage_state
    if coverage_state is None:
        return False
    return coverage_state.strip().casefold() == "indexed"


def derive_final_status(outcomes: Sequence[object]) -> str:
    if not outcomes:
        return "COMPLETED"

    try:
        successful = [
            outcome
            for outcome in outcomes
            if getattr(outcome, "submission_skipped", False)
            or (
                getattr(outcome, "submission_success", False)
                and getattr(outcome, "inspection_attempted", False)
                and getattr(outcome, "inspection_success", False)
            )
        ]
    except AttributeError:
        return "COMPLETED"

    if len(successful) == len(outcomes):
        return "COMPLETED"
    if successful:
        return "PARTIAL_FAILURE"
    return "FAILED"
