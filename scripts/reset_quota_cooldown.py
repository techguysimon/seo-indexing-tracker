"""Reset quota_last_429_at field for a website to resume processing immediately."""

from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy import update

from seo_indexing_tracker.config import get_settings
from seo_indexing_tracker.database import AsyncSessionFactory, close_database
from seo_indexing_tracker.models import Website


async def reset_quota_cooldown(website_id: str) -> bool:
    """Reset quota_last_429_at to NULL for the given website.

    Args:
        website_id: UUID string of the website to reset.

    Returns:
        True if a row was updated, False otherwise.
    """
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            update(Website)
            .where(Website.id == UUID(website_id))
            .values(quota_last_429_at=None)
        )
        await session.commit()
        return result.rowcount > 0


async def main() -> None:
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m scripts.reset_quota_cooldown <website_id>")
        print(
            "Example: python -m scripts.reset_quota_cooldown 84e504dc-e3ee-4c3b-b46d-2a598fe154d7"
        )
        sys.exit(1)

    website_id = sys.argv[1]
    settings = get_settings()

    print(f"Resetting quota cooldown for website {website_id}...")
    print(f"Database: {settings.DATABASE_URL}")

    success = await reset_quota_cooldown(website_id)

    if success:
        print(
            f"✓ Successfully reset quota_last_429_at to NULL for website {website_id}"
        )
        print("Processing can now resume immediately.")
    else:
        print(f"✗ No website found with ID {website_id}")
        sys.exit(1)

    await close_database()


if __name__ == "__main__":
    asyncio.run(main())
