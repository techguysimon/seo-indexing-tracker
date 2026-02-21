"""Aggregated index coverage statistics for websites and dashboard views."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from seo_indexing_tracker.models import Sitemap, URL, URLIndexStatus, Website


class IndexStatsService:
    """Query denormalized URL index status counters for observability surfaces."""

    @staticmethod
    async def get_website_index_stats(
        session: AsyncSession,
        website_id: UUID,
    ) -> dict[str, object]:
        """Return status aggregates and sitemap breakdown for one website."""

        totals = await IndexStatsService._status_totals_query(
            session=session,
            website_id=website_id,
        )
        per_sitemap = await IndexStatsService._per_sitemap_query(
            session=session,
            website_id=website_id,
        )
        return {
            **totals,
            "website_id": str(website_id),
            "per_sitemap": per_sitemap,
        }

    @staticmethod
    async def get_dashboard_index_stats(session: AsyncSession) -> dict[str, object]:
        """Return cross-website aggregate coverage plus per-website breakdown."""

        totals = await IndexStatsService._status_totals_query(session=session)
        per_website_rows = await session.execute(
            select(
                Website.id.label("website_id"),
                Website.domain.label("domain"),
                func.count(URL.id).label("total_urls"),
                func.sum(
                    case(
                        (URL.latest_index_status == URLIndexStatus.INDEXED, 1), else_=0
                    )
                ).label("indexed_count"),
                func.sum(
                    case(
                        (URL.latest_index_status == URLIndexStatus.NOT_INDEXED, 1),
                        else_=0,
                    )
                ).label("not_indexed_count"),
                func.sum(
                    case(
                        (URL.latest_index_status == URLIndexStatus.BLOCKED, 1), else_=0
                    )
                ).label("blocked_count"),
                func.sum(
                    case(
                        (URL.latest_index_status == URLIndexStatus.SOFT_404, 1), else_=0
                    )
                ).label("soft_404_count"),
                func.sum(
                    case((URL.latest_index_status == URLIndexStatus.ERROR, 1), else_=0)
                ).label("error_count"),
                func.sum(
                    case(
                        (URL.latest_index_status == URLIndexStatus.UNCHECKED, 1),
                        else_=0,
                    )
                ).label("unchecked_count"),
            )
            .outerjoin(URL, URL.website_id == Website.id)
            .group_by(Website.id, Website.domain)
            .order_by(Website.domain.asc())
        )

        per_website: list[dict[str, object]] = []
        for row in per_website_rows:
            website_total = int(row.total_urls or 0)
            indexed_count = int(row.indexed_count or 0)
            per_website.append(
                {
                    "website_id": str(row.website_id),
                    "domain": row.domain,
                    "total_urls": website_total,
                    "indexed_count": indexed_count,
                    "not_indexed_count": int(row.not_indexed_count or 0),
                    "blocked_count": int(row.blocked_count or 0),
                    "soft_404_count": int(row.soft_404_count or 0),
                    "error_count": int(row.error_count or 0),
                    "unchecked_count": int(row.unchecked_count or 0),
                    "coverage_percentage": IndexStatsService._coverage_percentage(
                        indexed_count=indexed_count,
                        total_urls=website_total,
                    ),
                }
            )

        return {**totals, "per_website": per_website}

    @staticmethod
    async def _status_totals_query(
        *,
        session: AsyncSession,
        website_id: UUID | None = None,
    ) -> dict[str, object]:
        statement = select(
            func.count(URL.id).label("total_urls"),
            func.sum(
                case((URL.latest_index_status == URLIndexStatus.INDEXED, 1), else_=0)
            ).label("indexed_count"),
            func.sum(
                case(
                    (URL.latest_index_status == URLIndexStatus.NOT_INDEXED, 1), else_=0
                )
            ).label("not_indexed_count"),
            func.sum(
                case((URL.latest_index_status == URLIndexStatus.BLOCKED, 1), else_=0)
            ).label("blocked_count"),
            func.sum(
                case((URL.latest_index_status == URLIndexStatus.SOFT_404, 1), else_=0)
            ).label("soft_404_count"),
            func.sum(
                case((URL.latest_index_status == URLIndexStatus.ERROR, 1), else_=0)
            ).label("error_count"),
            func.sum(
                case((URL.latest_index_status == URLIndexStatus.UNCHECKED, 1), else_=0)
            ).label("unchecked_count"),
        )
        if website_id is not None:
            statement = statement.where(URL.website_id == website_id)

        row = (await session.execute(statement)).one()
        total_urls = int(row.total_urls or 0)
        indexed_count = int(row.indexed_count or 0)
        return {
            "total_urls": total_urls,
            "indexed_count": indexed_count,
            "not_indexed_count": int(row.not_indexed_count or 0),
            "blocked_count": int(row.blocked_count or 0),
            "soft_404_count": int(row.soft_404_count or 0),
            "error_count": int(row.error_count or 0),
            "unchecked_count": int(row.unchecked_count or 0),
            "coverage_percentage": IndexStatsService._coverage_percentage(
                indexed_count=indexed_count,
                total_urls=total_urls,
            ),
        }

    @staticmethod
    async def _per_sitemap_query(
        *,
        session: AsyncSession,
        website_id: UUID,
    ) -> list[dict[str, object]]:
        rows = await session.execute(
            select(
                URL.sitemap_id.label("sitemap_id"),
                Sitemap.url.label("sitemap_url"),
                func.count(URL.id).label("total_urls"),
                func.sum(
                    case(
                        (URL.latest_index_status == URLIndexStatus.INDEXED, 1), else_=0
                    )
                ).label("indexed_count"),
                func.sum(
                    case(
                        (URL.latest_index_status == URLIndexStatus.NOT_INDEXED, 1),
                        else_=0,
                    )
                ).label("not_indexed_count"),
                func.sum(
                    case(
                        (URL.latest_index_status == URLIndexStatus.BLOCKED, 1), else_=0
                    )
                ).label("blocked_count"),
                func.sum(
                    case(
                        (URL.latest_index_status == URLIndexStatus.SOFT_404, 1), else_=0
                    )
                ).label("soft_404_count"),
                func.sum(
                    case((URL.latest_index_status == URLIndexStatus.ERROR, 1), else_=0)
                ).label("error_count"),
                func.sum(
                    case(
                        (URL.latest_index_status == URLIndexStatus.UNCHECKED, 1),
                        else_=0,
                    )
                ).label("unchecked_count"),
            )
            .outerjoin(Sitemap, Sitemap.id == URL.sitemap_id)
            .where(URL.website_id == website_id)
            .group_by(URL.sitemap_id, Sitemap.url)
            .order_by(func.count(URL.id).desc(), Sitemap.url.asc())
        )

        breakdown: list[dict[str, object]] = []
        for row in rows:
            total_urls = int(row.total_urls or 0)
            indexed_count = int(row.indexed_count or 0)
            sitemap_url = (
                row.sitemap_url if row.sitemap_url is not None else "Unassigned"
            )
            breakdown.append(
                {
                    "sitemap_id": str(row.sitemap_id)
                    if row.sitemap_id is not None
                    else None,
                    "sitemap_url": sitemap_url,
                    "total_urls": total_urls,
                    "indexed_count": indexed_count,
                    "not_indexed_count": int(row.not_indexed_count or 0),
                    "blocked_count": int(row.blocked_count or 0),
                    "soft_404_count": int(row.soft_404_count or 0),
                    "error_count": int(row.error_count or 0),
                    "unchecked_count": int(row.unchecked_count or 0),
                    "coverage_percentage": IndexStatsService._coverage_percentage(
                        indexed_count=indexed_count,
                        total_urls=total_urls,
                    ),
                }
            )

        return breakdown

    @staticmethod
    def _coverage_percentage(*, indexed_count: int, total_urls: int) -> float:
        if total_urls <= 0:
            return 0.0
        return round((indexed_count / total_urls) * 100.0, 2)


__all__ = ["IndexStatsService"]
