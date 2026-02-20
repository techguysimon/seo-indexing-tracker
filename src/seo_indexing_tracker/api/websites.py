"""Website CRUD API routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.models import Website
from seo_indexing_tracker.schemas import (
    WebsiteCreate,
    WebsiteDetailRead,
    WebsiteRateLimitRead,
    WebsiteRateLimitUpdate,
    WebsiteRead,
    WebsiteUpdate,
)
from seo_indexing_tracker.services.config_validation import (
    ConfigurationValidationError,
    ConfigurationValidationService,
)

router = APIRouter(prefix="/api/websites", tags=["websites"])

config_validation_service = ConfigurationValidationService()


def _websites_query(*, is_active: bool | None) -> Select[tuple[Website]]:
    statement = select(Website).options(
        selectinload(Website.service_account),
        selectinload(Website.sitemaps),
    )
    if is_active is None:
        return statement.order_by(Website.domain)

    return statement.where(Website.is_active == is_active).order_by(Website.domain)


async def _get_website_or_404(*, website_id: UUID, session: AsyncSession) -> Website:
    statement = (
        select(Website)
        .options(selectinload(Website.service_account), selectinload(Website.sitemaps))
        .where(Website.id == website_id)
    )
    website = await session.scalar(statement)
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found",
        )
    return website


def _serialize_rate_limit_config(website: Website) -> WebsiteRateLimitRead:
    return WebsiteRateLimitRead.model_validate(
        {
            "website_id": website.id,
            "rate_limit_bucket_size": website.rate_limit_bucket_size,
            "rate_limit_refill_rate": website.rate_limit_refill_rate,
            "rate_limit_max_concurrent_requests": website.rate_limit_max_concurrent_requests,
            "rate_limit_queue_excess_requests": website.rate_limit_queue_excess_requests,
        }
    )


@router.post("", response_model=WebsiteRead, status_code=status.HTTP_201_CREATED)
async def create_website(
    payload: WebsiteCreate,
    session: AsyncSession = Depends(get_db_session),
) -> Website:
    site_url = str(payload.site_url)
    try:
        await config_validation_service.validate_website_url(site_url=site_url)
    except ConfigurationValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error

    website = Website(
        domain=payload.domain,
        site_url=site_url,
        is_active=payload.is_active,
        rate_limit_bucket_size=payload.rate_limit_bucket_size,
        rate_limit_refill_rate=payload.rate_limit_refill_rate,
        rate_limit_max_concurrent_requests=payload.rate_limit_max_concurrent_requests,
        rate_limit_queue_excess_requests=payload.rate_limit_queue_excess_requests,
    )
    session.add(website)

    try:
        await session.flush()
    except IntegrityError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Website with this domain already exists",
        ) from error

    return await _get_website_or_404(website_id=website.id, session=session)


@router.get("", response_model=list[WebsiteRead], status_code=status.HTTP_200_OK)
async def list_websites(
    is_active: bool | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> list[Website]:
    statement = _websites_query(is_active=is_active)
    result = await session.scalars(statement)
    return list(result)


@router.get(
    "/{website_id}", response_model=WebsiteDetailRead, status_code=status.HTTP_200_OK
)
async def get_website(
    website_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> WebsiteDetailRead:
    website = await _get_website_or_404(website_id=website_id, session=session)
    return WebsiteDetailRead.model_validate(
        {
            "id": website.id,
            "domain": website.domain,
            "site_url": website.site_url,
            "is_active": website.is_active,
            "rate_limit_bucket_size": website.rate_limit_bucket_size,
            "rate_limit_refill_rate": website.rate_limit_refill_rate,
            "rate_limit_max_concurrent_requests": website.rate_limit_max_concurrent_requests,
            "rate_limit_queue_excess_requests": website.rate_limit_queue_excess_requests,
            "created_at": website.created_at,
            "updated_at": website.updated_at,
            "service_account": website.service_account,
            "service_account_count": int(website.service_account is not None),
            "sitemap_count": len(website.sitemaps),
        }
    )


@router.put("/{website_id}", response_model=WebsiteRead, status_code=status.HTTP_200_OK)
async def update_website(
    website_id: UUID,
    payload: WebsiteUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> Website:
    website = await _get_website_or_404(website_id=website_id, session=session)

    update_data = payload.model_dump(exclude_unset=True)
    if "site_url" in update_data:
        site_url = str(update_data["site_url"])
        try:
            await config_validation_service.validate_website_url(site_url=site_url)
        except ConfigurationValidationError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(error),
            ) from error
        update_data["site_url"] = site_url

    for field_name, field_value in update_data.items():
        setattr(website, field_name, field_value)

    try:
        await session.flush()
    except IntegrityError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Website with this domain already exists",
        ) from error

    return await _get_website_or_404(website_id=website.id, session=session)


@router.delete(
    "/{website_id}",
    response_model=WebsiteRead,
    status_code=status.HTTP_200_OK,
)
async def delete_website(
    website_id: UUID,
    hard_delete: bool = Query(default=False),
    session: AsyncSession = Depends(get_db_session),
) -> Website | Response:
    website = await _get_website_or_404(website_id=website_id, session=session)

    if hard_delete:
        await session.delete(website)
        await session.flush()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    website.is_active = False
    await session.flush()
    return await _get_website_or_404(website_id=website.id, session=session)


@router.get(
    "/{website_id}/rate-limit",
    response_model=WebsiteRateLimitRead,
    status_code=status.HTTP_200_OK,
)
async def get_rate_limit_config(
    website_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> WebsiteRateLimitRead:
    website = await _get_website_or_404(website_id=website_id, session=session)
    return _serialize_rate_limit_config(website)


@router.put(
    "/{website_id}/rate-limit",
    response_model=WebsiteRateLimitRead,
    status_code=status.HTTP_200_OK,
)
async def update_rate_limit_config(
    website_id: UUID,
    payload: WebsiteRateLimitUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> WebsiteRateLimitRead:
    website = await _get_website_or_404(website_id=website_id, session=session)
    update_data = payload.model_dump(exclude_unset=True)
    for field_name, field_value in update_data.items():
        setattr(website, field_name, field_value)

    await session.flush()
    return _serialize_rate_limit_config(website)
