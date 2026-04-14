"""Utilities for shared application concerns."""

from seo_indexing_tracker import __version__
from seo_indexing_tracker.utils.logging import (
    add_request_logging_middleware,
    setup_logging,
)
from seo_indexing_tracker.utils.form_helpers import (
    _form_bool,
    _form_float,
    _form_int,
    _form_uuid,
)
from seo_indexing_tracker.utils.shared_helpers import (
    extract_index_status_result,
    optional_text,
    parse_verdict,
)

__all__ = [
    "__version__",
    "add_request_logging_middleware",
    "setup_logging",
    "extract_index_status_result",
    "optional_text",
    "parse_verdict",
    "_form_bool",
    "_form_float",
    "_form_int",
    "_form_uuid",
]
