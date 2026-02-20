"""Utilities for shared application concerns."""

from seo_indexing_tracker import __version__
from seo_indexing_tracker.utils.logging import (
    add_request_logging_middleware,
    setup_logging,
)

__all__ = ["__version__", "add_request_logging_middleware", "setup_logging"]
