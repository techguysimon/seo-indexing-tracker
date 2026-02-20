"""API package exports."""

from seo_indexing_tracker import __version__
from seo_indexing_tracker.api.config_validation import (
    router as config_validation_router,
)
from seo_indexing_tracker.api.queue import router as queue_router
from seo_indexing_tracker.api.scheduler import router as scheduler_router
from seo_indexing_tracker.api.service_accounts import router as service_accounts_router
from seo_indexing_tracker.api.sitemaps import router as sitemaps_router
from seo_indexing_tracker.api.web import router as web_router
from seo_indexing_tracker.api.websites import router as websites_router

__all__ = [
    "__version__",
    "config_validation_router",
    "queue_router",
    "scheduler_router",
    "service_accounts_router",
    "sitemaps_router",
    "web_router",
    "websites_router",
]
