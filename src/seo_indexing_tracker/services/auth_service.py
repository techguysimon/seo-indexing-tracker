"""Authentication service: Google OAuth + JWT + email allowlist."""

from __future__ import annotations

import logging
import secrets
import time

from authlib.integrations.starlette_client import OAuth
from jose import JWTError, jwt
from pydantic import SecretStr

from seo_indexing_tracker.config import get_settings
from seo_indexing_tracker.schemas.auth import OAuthCallbackResult

logger = logging.getLogger("seo_indexing_tracker.auth")


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
        self._oauth_client: OAuth | None = None
        self._oauth_initialized: bool = False

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

    def _get_oauth_client(self) -> OAuth:
        if self._oauth_client is None:
            self._oauth_client = OAuth()
            self._oauth_client.register(
                name="google",
                client_id=self._google_client_id,
                client_secret=self._google_client_secret,
                server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                client_kwargs={"scope": "openid email profile"},
            )
            self._oauth_initialized = True
        return self._oauth_client

    def get_google_authorization_url(self, request) -> str:
        oauth = self._get_oauth_client()
        redirect_uri = str(request.url_for("auth_callback"))
        try:
            result = oauth.google.create_authorization_url(redirect_uri)
            if isinstance(result, tuple):
                return result[0]
            return result.get("url", "")
        except Exception as e:
            logger.error("Failed to create Google authorization URL: %s", e)
            raise

    async def fetch_google_user_info(self, code: str, request) -> dict:
        oauth = self._get_oauth_client()
        redirect_uri = str(request.url_for("auth_callback"))
        token = await oauth.google.fetch_token(redirect_uri, code=code)
        user_info = await oauth.google.get("userinfo", token=token)
        return user_info.json()

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
