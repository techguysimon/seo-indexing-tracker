"""Sitemap CRUD API routes."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.database import get_db_session
from seo_indexing_tracker.models import Sitemap, Website
from seo_indexing_tracker.schemas import SitemapCreate, SitemapRead, SitemapUpdate
from seo_indexing_tracker.services import (
    SitemapFetchError,
    SitemapTypeDetectionError,
    detect_sitemap_type,
    fetch_sitemap,
)
from seo_indexing_tracker.services.config_validation import (
    ConfigurationValidationError,
    ConfigurationValidationService,
)

router = APIRouter(prefix="/api", tags=["sitemaps"])

config_validation_service = ConfigurationValidationService()


async def _ensure_website_exists(*, website_id: UUID, session: AsyncSession) -> None:
    website = await session.scalar(select(Website.id).where(Website.id == website_id))
    if website is not None:
        return

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Website not found",
    )


async def _get_sitemap_or_404(*, sitemap_id: UUID, session: AsyncSession) -> Sitemap:
    sitemap = await session.scalar(select(Sitemap).where(Sitemap.id == sitemap_id))
    if sitemap is not None:
        return sitemap

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Sitemap not found",
    )


async def _build_sitemap_for_creation(
    *,
    website_id: UUID,
    payload: SitemapCreate,
) -> Sitemap:
    if payload.sitemap_type is not None:
        return Sitemap(
            website_id=website_id,
            url=str(payload.url),
            sitemap_type=payload.sitemap_type,
            is_active=payload.is_active,
        )

    try:
        fetch_result = await fetch_sitemap(str(payload.url))
    except SitemapFetchError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unable to fetch sitemap for type detection: {error}",
        ) from error

    try:
        detected_type = detect_sitemap_type(fetch_result.content or b"")
    except SitemapTypeDetectionError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unable to detect sitemap type: {error}",
        ) from error

    return Sitemap(
        website_id=website_id,
        url=str(payload.url),
        sitemap_type=detected_type,
        is_active=payload.is_active,
        etag=fetch_result.etag,
        last_modified_header=fetch_result.last_modified,
        last_fetched=datetime.now(UTC),
    )


@router.post(
    "/websites/{website_id}/sitemaps",
    response_model=SitemapRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_sitemap(
    website_id: UUID,
    payload: SitemapCreate,
    session: AsyncSession = Depends(get_db_session),
) -> Sitemap:
    await _ensure_website_exists(website_id=website_id, session=session)

    try:
        await config_validation_service.validate_sitemap_url(
            sitemap_url=str(payload.url)
        )
    except ConfigurationValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error

    sitemap = await _build_sitemap_for_creation(website_id=website_id, payload=payload)
    session.add(sitemap)

    try:
        await session.flush()
    except IntegrityError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Sitemap already exists for this website",
        ) from error

    return await _get_sitemap_or_404(sitemap_id=sitemap.id, session=session)


@router.get(
    "/websites/{website_id}/sitemaps",
    response_model=list[SitemapRead],
    status_code=status.HTTP_200_OK,
)
async def list_sitemaps(
    website_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> list[Sitemap]:
    await _ensure_website_exists(website_id=website_id, session=session)
    result = await session.scalars(
        select(Sitemap)
        .where(Sitemap.website_id == website_id)
        .order_by(Sitemap.created_at.desc())
    )
    return list(result)


@router.get(
    "/sitemaps/{sitemap_id}",
    response_model=SitemapRead,
    status_code=status.HTTP_200_OK,
)
async def get_sitemap(
    sitemap_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> Sitemap:
    return await _get_sitemap_or_404(sitemap_id=sitemap_id, session=session)


@router.put(
    "/sitemaps/{sitemap_id}",
    response_model=SitemapRead,
    status_code=status.HTTP_200_OK,
)
async def update_sitemap(
    sitemap_id: UUID,
    payload: SitemapUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> Sitemap:
    sitemap = await _get_sitemap_or_404(sitemap_id=sitemap_id, session=session)
    update_data = payload.model_dump(exclude_unset=True)

    if "url" in update_data:
        sitemap_url = str(update_data["url"])
        try:
            await config_validation_service.validate_sitemap_url(
                sitemap_url=sitemap_url
            )
        except ConfigurationValidationError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(error),
            ) from error
        update_data["url"] = sitemap_url

    for field_name, field_value in update_data.items():
        setattr(sitemap, field_name, field_value)

    try:
        await session.flush()
    except IntegrityError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Sitemap already exists for this website",
        ) from error

    return await _get_sitemap_or_404(sitemap_id=sitemap.id, session=session)


@router.delete("/sitemaps/{sitemap_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sitemap(
    sitemap_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    sitemap = await _get_sitemap_or_404(sitemap_id=sitemap_id, session=session)
    await session.delete(sitemap)
    await session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
