"""Shared helper functions for API response parsing and verdict conversion."""

from typing import Any

from seo_indexing_tracker.models import IndexVerdict


def parse_verdict(verdict: str | None) -> IndexVerdict:
    """Convert verdict string to IndexVerdict enum.

    Args:
        verdict: The verdict string from API response, may be None.

    Returns:
        IndexVerdict enum member, defaults to NEUTRAL if not recognized.
    """
    if verdict is None:
        return IndexVerdict.NEUTRAL

    normalized_verdict = verdict.strip().upper()
    if normalized_verdict in {
        IndexVerdict.PASS.value,
        IndexVerdict.FAIL.value,
        IndexVerdict.NEUTRAL.value,
        IndexVerdict.PARTIAL.value,
    }:
        return IndexVerdict(normalized_verdict)

    return IndexVerdict.NEUTRAL


def extract_index_status_result(raw_response: dict[str, Any]) -> dict[str, Any]:
    """Extract indexStatusResult from raw API response.

    Args:
        raw_response: The raw API response dictionary.

    Returns:
        The indexStatusResult dict, or empty dict if not found.
    """
    inspection_result = raw_response.get("inspectionResult")
    if not isinstance(inspection_result, dict):
        return {}

    index_status_result = inspection_result.get("indexStatusResult")
    if not isinstance(index_status_result, dict):
        return {}

    return index_status_result


def optional_text(value: Any) -> str | None:
    """Extract optional text from response value.

    Args:
        value: The value from API response.

    Returns:
        Stripped string if non-empty, otherwise None.
    """
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return None
