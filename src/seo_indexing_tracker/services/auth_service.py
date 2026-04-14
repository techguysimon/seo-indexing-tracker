"""Authentication service: Google OAuth + JWT + email allowlist."""

from __future__ import annotations

import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
from jose import JWTError, jwt
from pydantic import SecretStr

from seo_indexing_tracker.config import get_settings
from seo_indexing_tracker.schemas.auth import OAuthCallbackResult

logger = logging.getLogger("seo_indexing_tracker.auth")

_GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"
_GOOGLE_DISCOVERY_ENDPOINT = (
    "https://accounts.google.com/.well-known/openid-configuration"
)


class AuthService:
    """Handles Google OAuth, JWT tokens, and email role resolution."""

    _instance: AuthService | None = None

    def __init__(self) -> None:
        settings = get_settings()
        self._google_client_id = settings.GOOGLE_CLIENT_ID
        self._google_client_secret = settings.GOOGLE_CLIENT_SECRET.get_secret_value()
        self._admin_emails = settings.admin_email_list
        self._guest_emails = settings.guest_email_list
        self._jwt_secret = self._get_or_create_jwt_secret(settings.JWT_SECRET_KEY)
        self._jwt_expiry_hours = settings.JWT_EXPIRY_HOURS

        if self._google_client_id and self._google_client_secret:
            logger.info("Google OAuth configured (client_id set)")
        else:
            logger.warning(
                "Google OAuth NOT configured (client_id or client_secret missing)"
            )

    @staticmethod
    def _get_or_create_jwt_secret(secret: SecretStr) -> str:
        val = secret.get_secret_value()
        if val:
            return val
        generated = secrets.token_urlsafe(32)
        logger.warning(
            "JWT_SECRET_KEY not set. Generated ephemeral key: %s", generated[:8]
        )
        return generated

    @classmethod
    def get_instance(cls) -> AuthService:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Google OAuth ────────────────────────────────────────────────────────

    def build_authorization_url(self, request) -> str:
        """Build Google OAuth authorization URL manually."""
        redirect_uri = str(request.url_for("auth_callback"))
        params = {
            "client_id": self._google_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{_GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"

    async def fetch_google_user_info(self, code: str, request) -> dict:
        """Exchange code for tokens and return user info dict."""
        redirect_uri = str(request.url_for("auth_callback"))

        async with httpx.AsyncClient() as client:
            # Exchange code for tokens
            token_resp = await client.post(
                _GOOGLE_TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": self._google_client_id,
                    "client_secret": self._google_client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            token_resp.raise_for_status()
            token_data = token_resp.json()
            logger.info("Token response: %s", list(token_data.keys()))

            access_token = token_data.get("access_token", "")
            id_token = token_data.get("id_token", "")

            # Fetch userinfo
            userinfo_resp = await client.get(
                _GOOGLE_USERINFO_ENDPOINT,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            userinfo_resp.raise_for_status()
            return userinfo_resp.json()

    # ── JWT ─────────────────────────────────────────────────────────────────

    def create_jwt(self, email: str, role: str) -> str:
        """Create JWT with email + role, signed with JWT_SECRET."""
        now = int(time.time())
        payload = {
            "sub": email,
            "role": role,
            "iat": now,
            "exp": now + (self._jwt_expiry_hours * 3600),
        }
        return jwt.encode(payload, self._jwt_secret, algorithm="HS256")

    @staticmethod
    def decode_jwt(token: str, secret: str) -> dict | None:
        """Decode and validate JWT. Return payload or None if invalid."""
        try:
            return jwt.decode(token, secret, algorithms=["HS256"])
        except JWTError:
            return None

    # ── Role resolution ─────────────────────────────────────────────────────

    def resolve_role(self, email: str) -> str:
        """Resolve email to role: admin > guest > stranger."""
        if email in self._admin_emails:
            return "admin"
        if email in self._guest_emails:
            return "guest"
        return "stranger"

    def create_callback_result(
        self, email: str, redirect_to: str = "/"
    ) -> OAuthCallbackResult:
        """Create callback result after email validation. Does not create JWT."""
        role = self.resolve_role(email)
        if role == "stranger":
            return OAuthCallbackResult(
                success=False,
                redirect_url="/access-denied",
                error_message=f"Email {email} not authorized",
            )
        return OAuthCallbackResult(
            success=True,
            redirect_url=redirect_to,
        )
