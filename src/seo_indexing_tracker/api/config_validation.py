"""API endpoint for on-demand configuration validation."""

from __future__ import annotations

from fastapi import APIRouter, status

from seo_indexing_tracker.schemas.config_validation import (
    ConfigurationValidationRequest,
    ConfigurationValidationResponse,
    ValidationResult,
)
from seo_indexing_tracker.services.config_validation import (
    ConfigurationValidationError,
    ConfigurationValidationService,
)

router = APIRouter(prefix="/api/config-validation", tags=["config-validation"])

config_validation_service = ConfigurationValidationService()


@router.post(
    "", response_model=ConfigurationValidationResponse, status_code=status.HTTP_200_OK
)
async def run_configuration_validation(
    payload: ConfigurationValidationRequest,
) -> ConfigurationValidationResponse:
    service_account_result: ValidationResult | None = None
    sitemap_url_result: ValidationResult | None = None
    website_url_result: ValidationResult | None = None

    if payload.credentials_path is not None:
        try:
            await config_validation_service.validate_service_account(
                credentials_path=payload.credentials_path,
                scopes=payload.scopes,
            )
            service_account_result = ValidationResult(
                valid=True,
                detail="Service account credentials are valid",
            )
        except ConfigurationValidationError as error:
            service_account_result = ValidationResult(valid=False, detail=str(error))

    if payload.sitemap_url is not None:
        sitemap_url = str(payload.sitemap_url)
        try:
            await config_validation_service.validate_sitemap_url(
                sitemap_url=sitemap_url
            )
            sitemap_url_result = ValidationResult(
                valid=True,
                detail="Sitemap URL is reachable",
            )
        except ConfigurationValidationError as error:
            sitemap_url_result = ValidationResult(valid=False, detail=str(error))

    if payload.website_url is not None:
        website_url = str(payload.website_url)
        try:
            await config_validation_service.validate_website_url(site_url=website_url)
            website_url_result = ValidationResult(
                valid=True,
                detail="Website URL is reachable",
            )
        except ConfigurationValidationError as error:
            website_url_result = ValidationResult(valid=False, detail=str(error))

    is_valid = all(
        check_result.valid
        for check_result in [
            service_account_result,
            sitemap_url_result,
            website_url_result,
        ]
        if check_result is not None
    )

    return ConfigurationValidationResponse(
        valid=is_valid,
        service_account=service_account_result,
        sitemap_url=sitemap_url_result,
        website_url=website_url_result,
    )


__all__ = ["router"]
