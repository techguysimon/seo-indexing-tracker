"""Service layer helpers for SEO Indexing Tracker."""

from seo_indexing_tracker import __version__
from seo_indexing_tracker.services.sitemap_fetcher import (
    SitemapFetchDecompressionError,
    SitemapFetchError,
    SitemapFetchHTTPError,
    SitemapFetchNetworkError,
    SitemapFetchResult,
    SitemapFetchTimeoutError,
    fetch_sitemap,
)
from seo_indexing_tracker.services.sitemap_decompressor import (
    SitemapDecompressionError,
    decompress_gzipped_content,
    decompress_gzipped_stream,
    is_gzipped_sitemap,
)
from seo_indexing_tracker.services.sitemap_type_detector import (
    SitemapTypeDetectionError,
    SitemapXMLParseError,
    UnknownSitemapTypeError,
    detect_sitemap_type,
)
from seo_indexing_tracker.services.sitemap_index_parser import (
    DEFAULT_MAX_DEPTH,
    SitemapCircularReference,
    SitemapDiscoveryRecord,
    SitemapIndexParseErrorRecord,
    SitemapIndexParseProgress,
    SitemapIndexParseResult,
    SitemapIndexParserError,
    SitemapIndexRootTypeError,
    SitemapIndexXMLParseError,
    parse_sitemap_index,
)
from seo_indexing_tracker.services.sitemap_url_parser import (
    SitemapURLParserError,
    SitemapURLRecord,
    SitemapURLXMLParseError,
    parse_sitemap_urls_stream,
)
from seo_indexing_tracker.services.url_discovery import (
    URLDiscoveryResult,
    URLDiscoveryService,
)
from seo_indexing_tracker.services.priority_queue import (
    PriorityQueueService,
    calculate_url_priority,
)
from seo_indexing_tracker.services.google_credentials import (
    GoogleCredentialsError,
    clear_google_credentials_cache,
    load_service_account_credentials,
)
from seo_indexing_tracker.services.google_indexing_client import (
    BatchSubmitResult,
    GoogleIndexingClient,
    IndexingURLResult,
    MetadataLookupResult,
)
from seo_indexing_tracker.services.google_url_inspection_client import (
    GoogleURLInspectionClient,
    IndexStatusResult,
    InspectionSystemStatus,
)
from seo_indexing_tracker.services.google_api_factory import (
    GoogleAPIClientFactory,
    WebsiteGoogleAPIClients,
    WebsiteServiceAccountConfig,
)
from seo_indexing_tracker.services.google_errors import (
    AuthenticationError,
    GoogleAPIError,
    InvalidURLError,
    QuotaExceededError,
    execute_with_google_retry,
    is_retryable_google_error,
    parse_google_http_error,
    retry_google_api_call,
)
from seo_indexing_tracker.services.config_validation import (
    ConfigurationValidationError,
    ConfigurationValidationService,
)
from seo_indexing_tracker.services.quota_service import (
    DailyQuotaExceededError,
    QuotaAPIType,
    QuotaService,
    QuotaServiceSettings,
)
from seo_indexing_tracker.services.rate_limiter import (
    ConcurrentRequestLimitExceededError,
    RateLimiterService,
    RateLimitPermit,
    RateLimitTimeoutError,
    RateLimitTokenUnavailableError,
    WebsiteRateLimitConfig,
)
from seo_indexing_tracker.services.batch_processor import (
    BatchProcessingResult,
    BatchProcessorService,
    BatchProcessorStatus,
    BatchProgressUpdate,
    URLBatchOutcome,
)
from seo_indexing_tracker.services.scheduler import (
    SchedulerJobState,
    SchedulerService,
)
from seo_indexing_tracker.services.processing_pipeline import (
    INDEX_VERIFICATION_JOB_ID,
    SITEMAP_REFRESH_JOB_ID,
    URL_SUBMISSION_JOB_ID,
    JobExecutionMetrics,
    SchedulerProcessingPipelineService,
    set_scheduler_processing_pipeline_service,
)

__all__ = [
    "__version__",
    "SitemapDecompressionError",
    "SitemapFetchDecompressionError",
    "SitemapFetchError",
    "SitemapFetchHTTPError",
    "SitemapFetchNetworkError",
    "SitemapFetchResult",
    "SitemapFetchTimeoutError",
    "DEFAULT_MAX_DEPTH",
    "SitemapCircularReference",
    "SitemapDiscoveryRecord",
    "SitemapIndexParseErrorRecord",
    "SitemapIndexParseProgress",
    "SitemapIndexParseResult",
    "SitemapIndexParserError",
    "SitemapIndexRootTypeError",
    "SitemapIndexXMLParseError",
    "SitemapURLParserError",
    "SitemapURLRecord",
    "SitemapURLXMLParseError",
    "SitemapTypeDetectionError",
    "SitemapXMLParseError",
    "PriorityQueueService",
    "GoogleCredentialsError",
    "URLDiscoveryResult",
    "URLDiscoveryService",
    "UnknownSitemapTypeError",
    "BatchSubmitResult",
    "GoogleIndexingClient",
    "GoogleURLInspectionClient",
    "GoogleAPIClientFactory",
    "ConfigurationValidationError",
    "ConfigurationValidationService",
    "DailyQuotaExceededError",
    "QuotaAPIType",
    "QuotaService",
    "QuotaServiceSettings",
    "ConcurrentRequestLimitExceededError",
    "RateLimiterService",
    "RateLimitPermit",
    "RateLimitTimeoutError",
    "RateLimitTokenUnavailableError",
    "WebsiteRateLimitConfig",
    "BatchProcessingResult",
    "BatchProcessorService",
    "BatchProcessorStatus",
    "BatchProgressUpdate",
    "URLBatchOutcome",
    "SchedulerJobState",
    "SchedulerService",
    "INDEX_VERIFICATION_JOB_ID",
    "JobExecutionMetrics",
    "SITEMAP_REFRESH_JOB_ID",
    "SchedulerProcessingPipelineService",
    "URL_SUBMISSION_JOB_ID",
    "set_scheduler_processing_pipeline_service",
    "IndexingURLResult",
    "IndexStatusResult",
    "InspectionSystemStatus",
    "WebsiteGoogleAPIClients",
    "WebsiteServiceAccountConfig",
    "MetadataLookupResult",
    "AuthenticationError",
    "GoogleAPIError",
    "InvalidURLError",
    "QuotaExceededError",
    "calculate_url_priority",
    "clear_google_credentials_cache",
    "decompress_gzipped_content",
    "decompress_gzipped_stream",
    "detect_sitemap_type",
    "execute_with_google_retry",
    "fetch_sitemap",
    "is_retryable_google_error",
    "is_gzipped_sitemap",
    "load_service_account_credentials",
    "parse_google_http_error",
    "parse_sitemap_index",
    "parse_sitemap_urls_stream",
    "retry_google_api_call",
]
