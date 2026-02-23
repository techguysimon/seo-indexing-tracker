"""Utility functions for index status derivation."""

from __future__ import annotations

from seo_indexing_tracker.models import URLIndexStatus


def derive_url_index_status_from_coverage_state(coverage_state: str) -> URLIndexStatus:
    """Derive URLIndexStatus from coverage_state string.

    Maps Google Search Console coverage states to internal URLIndexStatus enum.

    Args:
        coverage_state: The coverage state string from Google API response.

    Returns:
        The corresponding URLIndexStatus enum value.
    """
    normalized_coverage = coverage_state.strip().casefold()

    if normalized_coverage in {
        "indexed",
        "submitted and indexed",
        "alternate page with proper canonical tag",
    }:
        return URLIndexStatus.INDEXED

    if "soft 404" in normalized_coverage:
        return URLIndexStatus.SOFT_404

    if "blocked" in normalized_coverage or "robots" in normalized_coverage:
        return URLIndexStatus.BLOCKED

    if normalized_coverage in {"inspection_failed", "unknown", "error"}:
        return URLIndexStatus.ERROR

    return URLIndexStatus.NOT_INDEXED


__all__ = ["derive_url_index_status_from_coverage_state"]
