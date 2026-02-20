"""Tests for FastAPI template and static asset setup."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.routing import Mount

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.sqlite")
os.environ.setdefault("SECRET_KEY", "test-secret")

from seo_indexing_tracker.main import create_app


def test_create_app_configures_template_engine_and_static_mount() -> None:
    app = create_app()

    static_mounts = [
        route
        for route in app.routes
        if isinstance(route, Mount) and route.path == "/static"
    ]
    assert static_mounts

    templates = app.state.templates
    assert len(templates.context_processors) >= 2

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/",
            "raw_path": b"/",
            "root_path": "",
            "scheme": "http",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
            "app": app,
        }
    )

    context: dict[str, object] = {}
    for processor in templates.context_processors:
        context.update(processor(request))

    assert "current_user" in context
    assert "settings" in context


def test_dashboard_route_and_base_template_assets_are_present() -> None:
    app = create_app()

    dashboard_routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == "/" and "GET" in route.methods
    ]
    assert dashboard_routes

    templates_root = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "seo_indexing_tracker"
        / "templates"
    )
    base_template_content = (templates_root / "base.html").read_text(encoding="utf-8")

    assert "htmx.org" in base_template_content
    assert "alpinejs" in base_template_content
    assert '<header class="site-header">' in base_template_content
    assert '<footer class="site-footer">' in base_template_content
    assert "<nav" in base_template_content
