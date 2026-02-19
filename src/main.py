"""SEO Indexing Tracker - Main FastAPI application."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI(
    title="SEO Indexing Tracker",
    description="Monitor and manage URL indexing status across Google Search",
    version="0.1.0",
)

# Templates
templates = Jinja2Templates(directory="src/templates")

# Static files
app.mount("/static", StaticFiles(directory="src/static"), name="static")


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint - returns basic info."""
    return {"message": "SEO Indexing Tracker API", "version": "0.1.0"}


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy"}
