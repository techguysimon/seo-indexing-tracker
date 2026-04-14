"""Authentication API routes: login, OAuth callback, logout."""

import logging

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger("seo_indexing_tracker.auth")

from seo_indexing_tracker.schemas.auth import UserInfo
from seo_indexing_tracker.services.auth_service import AuthService

router = APIRouter(prefix="", tags=["auth"])
_auth_service = AuthService.get_instance()


# ── Dependencies ────────────────────────────────────────────────────────────


def get_current_user(request: Request) -> UserInfo | None:
    """Extract current user from request.state (set by auth middleware)."""
    user_info = getattr(request.state, "current_user", None)
    return user_info


def require_admin(request: Request) -> UserInfo:
    """Require admin role. Raise 403 if not admin."""
    from fastapi import HTTPException

    user = get_current_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required"
        )
    return user


# ── Routes ──────────────────────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
async def login(request: Request) -> HTMLResponse:
    """Render login page with Google OAuth URL."""
    templates: Jinja2Templates = request.app.state.templates
    google_url = _auth_service.build_authorization_url(request)
    return templates.TemplateResponse(
        request=request,
        name="pages/login.html",
        context={"google_auth_url": google_url},
    )


@router.get("/auth/callback")
async def auth_callback(request: Request) -> RedirectResponse:
    """Handle Google OAuth callback. Set JWT cookie and redirect."""
    code = request.query_params.get("code")
    error = request.query_params.get("error")

    if error or not code:
        return RedirectResponse(
            url="/access-denied?reason=oauth_error", status_code=302
        )

    try:
        user_info = await _auth_service.fetch_google_user_info(code, request)
        email = user_info.get("email", "")

        result = _auth_service.create_callback_result(email)

        if not result.success:
            return RedirectResponse(url=result.redirect_url, status_code=302)

        role = _auth_service.resolve_role(email)
        token = _auth_service.create_jwt(email, role)

        response = RedirectResponse(url=result.redirect_url, status_code=302)
        response.set_cookie(
            key="auth_token",
            value=token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=86400 * 7,
        )
        return response

    except Exception as e:
        logger.error(
            "OAuth callback error: %s (%s)",
            e,
            type(e).__name__,
            exc_info=True,
        )
        return RedirectResponse(
            url="/access-denied?reason=callback_error", status_code=302
        )


@router.get("/logout")
async def logout() -> RedirectResponse:
    """Clear auth cookie and redirect to login."""
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(key="auth_token")
    return response


@router.get("/access-denied", response_class=HTMLResponse)
async def access_denied(request: Request) -> HTMLResponse:
    """Render access denied page."""
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="pages/access_denied.html",
        context={},
    )
