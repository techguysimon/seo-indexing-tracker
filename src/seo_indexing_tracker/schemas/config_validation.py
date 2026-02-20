"""Schemas for configuration validation endpoint payloads."""

from __future__ import annotations

from pydantic import AnyHttpUrl, BaseModel, Field, model_validator


class ConfigurationValidationRequest(BaseModel):
    """Payload used to run ad hoc configuration validation checks."""

    credentials_path: str | None = Field(default=None, min_length=1, max_length=1024)
    scopes: list[str] = Field(default_factory=list)
    sitemap_url: AnyHttpUrl | None = None
    website_url: AnyHttpUrl | None = None

    @model_validator(mode="after")
    def ensure_validation_target_is_present(self) -> ConfigurationValidationRequest:
        if (
            self.credentials_path is None
            and self.sitemap_url is None
            and self.website_url is None
        ):
            raise ValueError(
                "Provide at least one validation target: credentials_path, sitemap_url, or website_url"
            )

        return self


class ValidationResult(BaseModel):
    """Result of a single validation check."""

    valid: bool
    detail: str


class ConfigurationValidationResponse(BaseModel):
    """Aggregated validation response for requested checks."""

    valid: bool
    service_account: ValidationResult | None = None
    sitemap_url: ValidationResult | None = None
    website_url: ValidationResult | None = None


__all__ = [
    "ConfigurationValidationRequest",
    "ConfigurationValidationResponse",
    "ValidationResult",
]
