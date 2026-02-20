"""Logging setup with structured output and request middleware."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from time import perf_counter
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from starlette.responses import Response

from seo_indexing_tracker.config import Settings

SENSITIVE_FIELD_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "authorization",
    "api_key",
    "apikey",
)


class SensitiveDataFilter(logging.Filter):
    """Redact sensitive fields in log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, dict):
            record.msg = _redact_payload(record.msg)

        if isinstance(record.args, dict):
            record.args = _redact_payload(record.args)

        return True


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted_payload: dict[str, Any] = {}
    for key, value in payload.items():
        if _is_sensitive_key(key):
            redacted_payload[key] = "[REDACTED]"
            continue

        if isinstance(value, dict):
            redacted_payload[key] = _redact_payload(value)
            continue

        redacted_payload[key] = value

    return redacted_payload


def _is_sensitive_key(key: str) -> bool:
    key_lower = key.lower()
    return any(marker in key_lower for marker in SENSITIVE_FIELD_MARKERS)


class JsonLogFormatter(logging.Formatter):
    """Format logs as JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": _format_timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        method = getattr(record, "method", None)
        if method is not None:
            payload["method"] = method

        path = getattr(record, "path", None)
        if path is not None:
            payload["path"] = path

        status_code = getattr(record, "status_code", None)
        if status_code is not None:
            payload["status_code"] = status_code

        duration_ms = getattr(record, "duration_ms", None)
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms

        client_ip = getattr(record, "client_ip", None)
        if client_ip is not None:
            payload["client_ip"] = client_ip

        return json.dumps(payload, default=str)


def _format_timestamp(created: float) -> str:
    return datetime.fromtimestamp(created, tz=UTC).isoformat()


def setup_logging(settings: Settings) -> None:
    """Configure application logging from runtime settings."""

    handler = _build_handler(settings)
    formatter = _build_formatter(settings)
    redaction_filter = SensitiveDataFilter()

    handler.setFormatter(formatter)
    handler.addFilter(redaction_filter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(getattr(logging, settings.LOG_LEVEL, logging.INFO))
    root_logger.addHandler(handler)

    logging.captureWarnings(True)


def _build_handler(settings: Settings) -> logging.Handler:
    if settings.LOG_FILE is None:
        return logging.StreamHandler()

    settings.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    return RotatingFileHandler(
        filename=settings.LOG_FILE,
        maxBytes=settings.LOG_FILE_MAX_BYTES,
        backupCount=settings.LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )


def _build_formatter(settings: Settings) -> logging.Formatter:
    if settings.LOG_FORMAT == "json":
        return JsonLogFormatter()

    return logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def add_request_logging_middleware(app: FastAPI) -> None:
    """Attach request logging middleware with timing metadata."""

    logger = logging.getLogger("seo_indexing_tracker.request")

    @app.middleware("http")
    async def log_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started_at = perf_counter()
        client_ip = request.client.host if request.client else "unknown"

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((perf_counter() - started_at) * 1000, 2)
            logger.exception(
                "request_failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": 500,
                    "duration_ms": duration_ms,
                    "client_ip": client_ip,
                },
            )
            raise

        duration_ms = round((perf_counter() - started_at) * 1000, 2)
        logger.info(
            "request_completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "client_ip": client_ip,
            },
        )
        return response
