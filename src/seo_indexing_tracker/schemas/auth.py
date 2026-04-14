"""Pydantic models for authentication."""

from pydantic import BaseModel, EmailStr


class UserInfo(BaseModel):
    """Authenticated user info extracted from JWT."""

    email: EmailStr
    role: str  # "admin" | "guest" | "stranger"


class LoginPageContext(BaseModel):
    """Context passed to login template."""

    google_auth_url: str
    access_denied: bool = False


class OAuthCallbackResult(BaseModel):
    """Result of OAuth callback processing."""

    success: bool
    redirect_url: str
    error_message: str | None = None
