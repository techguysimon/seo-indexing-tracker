"""Schema exports for API serialization."""

from seo_indexing_tracker import __version__
from seo_indexing_tracker.schemas.index_status import (
    IndexStatusBase,
    IndexStatusCreate,
    IndexStatusRead,
    IndexStatusUpdate,
)
from seo_indexing_tracker.schemas.service_account import (
    ServiceAccountBase,
    ServiceAccountCreate,
    ServiceAccountRead,
    ServiceAccountUpdate,
)
from seo_indexing_tracker.schemas.submission_log import (
    SubmissionLogBase,
    SubmissionLogFilter,
    SubmissionLogRead,
)
from seo_indexing_tracker.schemas.sitemap import (
    SitemapBase,
    SitemapCreate,
    SitemapRead,
    SitemapUpdate,
)
from seo_indexing_tracker.schemas.url import URLBase, URLCreate, URLRead, URLUpdate
from seo_indexing_tracker.schemas.website import (
    WebsiteBase,
    WebsiteCreate,
    WebsiteDetailRead,
    WebsiteRateLimitRead,
    WebsiteRateLimitUpdate,
    WebsiteRead,
    WebsiteUpdate,
)
from seo_indexing_tracker.schemas.config_validation import (
    ConfigurationValidationRequest,
    ConfigurationValidationResponse,
    ValidationResult,
)

__all__ = [
    "__version__",
    "IndexStatusBase",
    "IndexStatusCreate",
    "IndexStatusRead",
    "IndexStatusUpdate",
    "ServiceAccountBase",
    "ServiceAccountCreate",
    "ServiceAccountRead",
    "ServiceAccountUpdate",
    "SubmissionLogBase",
    "SubmissionLogFilter",
    "SubmissionLogRead",
    "SitemapBase",
    "SitemapCreate",
    "SitemapRead",
    "SitemapUpdate",
    "URLBase",
    "URLCreate",
    "URLRead",
    "URLUpdate",
    "WebsiteBase",
    "WebsiteCreate",
    "WebsiteDetailRead",
    "WebsiteRateLimitRead",
    "WebsiteRateLimitUpdate",
    "WebsiteRead",
    "WebsiteUpdate",
    "ConfigurationValidationRequest",
    "ConfigurationValidationResponse",
    "ValidationResult",
]
