"""Authentication middleware: extract JWT from cookie, set request.state."""

from __future__ import annotations

import logging
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from seo_indexing_tracker.services.auth_service import AuthService

logger = logging.getLogger("seo_indexing_tracker.auth")


# Paths that don't require any auth
PUBLIC_PATHS = {
    "/login",
    "/auth/callback",
    "/access-denied",
    "/health",
    "/static",
}


class AuthMiddleware(BaseHTTPMiddleware):
    """Extract JWT from cookie, set current_user on request.state."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._auth_service = AuthService.get_instance()
        self._jwt_secret = self._auth_service._jwt_secret

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip auth for public paths
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static"):
            request.state.current_user = None
            return await call_next(request)

        # Extract JWT from cookie
        token = request.cookies.get("auth_token")
        if not token:
            request.state.current_user = None
            return await call_next(request)

        # Decode and validate JWT
        payload = AuthService.decode_jwt(token, self._jwt_secret)
        if payload is None:
            request.state.current_user = None
            return await call_next(request)

        # Set user info on request.state
        from seo_indexing_tracker.schemas.auth import UserInfo

        user_info = UserInfo(
            email=payload.get("sub", ""),
            role=payload.get("role", "stranger"),
        )
        request.state.current_user = user_info
        return await call_next(request)
