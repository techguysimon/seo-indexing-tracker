"""Service account CRUD API routes scoped to a website."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.models import ServiceAccount, Website
from seo_indexing_tracker.schemas import (
    ServiceAccountBase,
    ServiceAccountRead,
    ServiceAccountUpdate,
)
from seo_indexing_tracker.services.config_validation import (
    ConfigurationValidationError,
    ConfigurationValidationService,
)

ALLOWED_SERVICE_ACCOUNT_SCOPES = frozenset({"indexing", "webmasters"})

router = APIRouter(
    prefix="/api/websites/{website_id}/service-account",
    tags=["service-accounts"],
)

config_validation_service = ConfigurationValidationService()


async def _ensure_website_exists(*, website_id: UUID, session: AsyncSession) -> None:
    statement = select(Website.id).where(Website.id == website_id)
    existing_website_id = await session.scalar(statement)
    if existing_website_id is not None:
        return

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Website not found",
    )


async def _get_service_account_or_404(
    *, website_id: UUID, session: AsyncSession
) -> ServiceAccount:
    statement = select(ServiceAccount).where(ServiceAccount.website_id == website_id)
    service_account = await session.scalar(statement)
    if service_account is not None:
        return service_account

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Service account not found",
    )


def _validate_scopes(*, scopes: list[str]) -> list[str]:
    normalized_scopes = [scope.strip().lower() for scope in scopes]
    if any(scope == "" for scope in normalized_scopes):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Scopes cannot contain empty values",
        )

    invalid_scopes = sorted(
        {
            scope
            for scope in normalized_scopes
            if scope not in ALLOWED_SERVICE_ACCOUNT_SCOPES
        }
    )
    if invalid_scopes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Invalid scopes: "
                f"{', '.join(invalid_scopes)}. Allowed scopes: indexing, webmasters"
            ),
        )

    unique_scopes: list[str] = []
    seen_scopes: set[str] = set()
    for scope in normalized_scopes:
        if scope in seen_scopes:
            continue

        unique_scopes.append(scope)
        seen_scopes.add(scope)
    return unique_scopes


@router.post("", response_model=ServiceAccountRead, status_code=status.HTTP_201_CREATED)
async def create_service_account(
    website_id: UUID,
    payload: ServiceAccountBase,
    session: AsyncSession = Depends(get_db_session),
) -> ServiceAccount:
    await _ensure_website_exists(website_id=website_id, session=session)

    existing_service_account_id = await session.scalar(
        select(ServiceAccount.id).where(ServiceAccount.website_id == website_id)
    )
    if existing_service_account_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Service account already exists for this website",
        )

    normalized_scopes = _validate_scopes(scopes=payload.scopes)
    try:
        credentials_path = await config_validation_service.validate_service_account(
            credentials_path=payload.credentials_path,
            scopes=normalized_scopes,
        )
    except ConfigurationValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error

    service_account = ServiceAccount(
        website_id=website_id,
        name=payload.name,
        credentials_path=credentials_path,
        scopes=normalized_scopes,
    )
    session.add(service_account)

    try:
        await session.flush()
    except IntegrityError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Service account already exists for this website",
        ) from error

    return await _get_service_account_or_404(website_id=website_id, session=session)


@router.get("", response_model=ServiceAccountRead, status_code=status.HTTP_200_OK)
async def get_service_account(
    website_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> ServiceAccount:
    await _ensure_website_exists(website_id=website_id, session=session)
    return await _get_service_account_or_404(website_id=website_id, session=session)


@router.put("", response_model=ServiceAccountRead, status_code=status.HTTP_200_OK)
async def update_service_account(
    website_id: UUID,
    payload: ServiceAccountUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> ServiceAccount:
    await _ensure_website_exists(website_id=website_id, session=session)
    service_account = await _get_service_account_or_404(
        website_id=website_id, session=session
    )

    update_data = payload.model_dump(exclude_unset=True)

    credentials_path = update_data.get("credentials_path")
    if credentials_path is not None:
        update_data["credentials_path"] = str(credentials_path)

    scopes = update_data.get("scopes")
    if scopes is not None:
        update_data["scopes"] = _validate_scopes(scopes=scopes)

    if "credentials_path" in update_data or "scopes" in update_data:
        credentials_path_to_validate = str(
            update_data.get("credentials_path", service_account.credentials_path)
        )
        scopes_to_validate = update_data.get("scopes", service_account.scopes)
        try:
            validated_credentials_path = (
                await config_validation_service.validate_service_account(
                    credentials_path=credentials_path_to_validate,
                    scopes=scopes_to_validate,
                )
            )
        except ConfigurationValidationError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(error),
            ) from error

        if "credentials_path" in update_data:
            update_data["credentials_path"] = validated_credentials_path

    for field_name, field_value in update_data.items():
        setattr(service_account, field_name, field_value)

    await session.flush()
    return await _get_service_account_or_404(website_id=website_id, session=session)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service_account(
    website_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await _ensure_website_exists(website_id=website_id, session=session)
    service_account = await _get_service_account_or_404(
        website_id=website_id, session=session
    )
    await session.delete(service_account)
    await session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
