"""Application entry point for SEO Indexing Tracker."""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

from seo_indexing_tracker.api.config_validation import (
    router as config_validation_router,
)
from seo_indexing_tracker.api.queue import router as queue_router
from seo_indexing_tracker.api.scheduler import router as scheduler_router
from seo_indexing_tracker.api.service_accounts import router as service_accounts_router
from seo_indexing_tracker.api.sitemaps import router as sitemaps_router
from seo_indexing_tracker.api.web import router as web_router
from seo_indexing_tracker.api.websites import router as websites_router
from seo_indexing_tracker.config import get_settings
from seo_indexing_tracker.database import close_database, initialize_database
from seo_indexing_tracker.services.processing_pipeline import (
    SchedulerProcessingPipelineService,
    set_scheduler_processing_pipeline_service,
)
from seo_indexing_tracker.services.scheduler import SchedulerService
from seo_indexing_tracker.utils.logging import (
    add_request_logging_middleware,
    setup_logging,
)

__all__ = ["app", "create_app", "main"]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    scheduler_service = SchedulerService.from_settings(settings)
    processing_pipeline_service = SchedulerProcessingPipelineService(
        scheduler=scheduler_service,
        settings=settings,
    )
    set_scheduler_processing_pipeline_service(processing_pipeline_service)
    app.state.scheduler_service = scheduler_service
    app.state.processing_pipeline_service = processing_pipeline_service

    await initialize_database()
    processing_pipeline_service.register_jobs()
    await scheduler_service.start()
    try:
        yield
    finally:
        await scheduler_service.shutdown()
        await close_database()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings)

    package_directory = Path(__file__).resolve().parent
    templates = Jinja2Templates(
        directory=str(package_directory / "templates"),
        context_processors=[
            lambda request: {
                "current_user": getattr(request.state, "current_user", None)
            },
            lambda request: {"settings": request.app.state.settings},
        ],
    )

    app = FastAPI(title="SEO Indexing Tracker", lifespan=lifespan)
    app.state.settings = settings
    app.state.templates = templates
    add_request_logging_middleware(app)
    app.mount(
        "/static",
        StaticFiles(directory=str(package_directory / "static")),
        name="static",
    )
    app.include_router(config_validation_router)
    app.include_router(web_router)
    app.include_router(queue_router)
    app.include_router(scheduler_router)
    app.include_router(websites_router)
    app.include_router(service_accounts_router)
    app.include_router(sitemaps_router)

    @app.get("/health", tags=["health"])
    async def health_check() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "seo_indexing_tracker.main:app",
        host=settings.HOST,
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(),
        reload=False,
    )
