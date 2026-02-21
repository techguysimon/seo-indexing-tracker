"""Tests for recursive sitemap index parsing."""

from __future__ import annotations

import pytest

from seo_indexing_tracker.services.sitemap_fetcher import SitemapFetchResult
from seo_indexing_tracker.services.sitemap_index_parser import (
    SitemapIndexRootTypeError,
    parse_sitemap_index,
)


def _fetch_result(url: str, content: str) -> SitemapFetchResult:
    return SitemapFetchResult(
        content=content.encode("utf-8"),
        etag=None,
        last_modified=None,
        status_code=200,
        content_type="application/xml",
        url=url,
        not_modified=False,
    )


@pytest.mark.asyncio
async def test_recursively_parses_child_sitemaps_and_tracks_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        "https://example.com/sitemap-index.xml": _fetch_result(
            "https://example.com/sitemap-index.xml",
            """
            <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <sitemap>
                    <loc>https://example.com/blog-sitemap.xml</loc>
                    <lastmod>2026-02-19</lastmod>
                </sitemap>
                <sitemap>
                    <loc>https://example.com/page-sitemap.xml</loc>
                    <lastmod>2026-02-18</lastmod>
                </sitemap>
            </sitemapindex>
            """,
        ),
        "https://example.com/blog-sitemap.xml": _fetch_result(
            "https://example.com/blog-sitemap.xml",
            """
            <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                <sitemap>
                    <loc>https://example.com/blog-posts-sitemap.xml</loc>
                    <lastmod>2026-02-17</lastmod>
                </sitemap>
            </sitemapindex>
            """,
        ),
        "https://example.com/page-sitemap.xml": _fetch_result(
            "https://example.com/page-sitemap.xml",
            "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'></urlset>",
        ),
        "https://example.com/blog-posts-sitemap.xml": _fetch_result(
            "https://example.com/blog-posts-sitemap.xml",
            "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'></urlset>",
        ),
    }

    async def fake_fetch_sitemap(url: str) -> SitemapFetchResult:
        return responses[url]

    monkeypatch.setattr(
        "seo_indexing_tracker.services.sitemap_index_parser.fetch_sitemap",
        fake_fetch_sitemap,
    )

    result = await parse_sitemap_index(
        "https://example.com/sitemap-index.xml", max_depth=3
    )

    assert result.all_sitemap_urls == [
        "https://example.com/sitemap-index.xml",
        "https://example.com/blog-sitemap.xml",
        "https://example.com/page-sitemap.xml",
        "https://example.com/blog-posts-sitemap.xml",
    ]

    blog_record = next(
        item
        for item in result.discovered_sitemaps
        if item.url == "https://example.com/blog-sitemap.xml"
    )
    assert blog_record.lastmod == "2026-02-19"
    assert blog_record.source_index_url == "https://example.com/sitemap-index.xml"
    assert blog_record.depth == 1

    nested_record = next(
        item
        for item in result.discovered_sitemaps
        if item.url == "https://example.com/blog-posts-sitemap.xml"
    )
    assert nested_record.lastmod == "2026-02-17"
    assert nested_record.source_index_url == "https://example.com/blog-sitemap.xml"
    assert nested_record.depth == 2

    assert result.progress.fetched_sitemaps == 4
    assert result.progress.parsed_indexes == 2
    assert result.progress.parsed_urlsets == 2
    assert result.progress.discovered_sitemaps == 4
    assert result.progress.circular_references == 0
    assert result.errors == []


@pytest.mark.asyncio
async def test_respects_max_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = {
        "https://example.com/root.xml": _fetch_result(
            "https://example.com/root.xml",
            """
            <sitemapindex>
                <sitemap><loc>https://example.com/level-1.xml</loc></sitemap>
            </sitemapindex>
            """,
        ),
        "https://example.com/level-1.xml": _fetch_result(
            "https://example.com/level-1.xml",
            """
            <sitemapindex>
                <sitemap><loc>https://example.com/level-2.xml</loc></sitemap>
            </sitemapindex>
            """,
        ),
    }

    async def fake_fetch_sitemap(url: str) -> SitemapFetchResult:
        return responses[url]

    monkeypatch.setattr(
        "seo_indexing_tracker.services.sitemap_index_parser.fetch_sitemap",
        fake_fetch_sitemap,
    )

    result = await parse_sitemap_index("https://example.com/root.xml", max_depth=1)

    assert result.all_sitemap_urls == [
        "https://example.com/root.xml",
        "https://example.com/level-1.xml",
        "https://example.com/level-2.xml",
    ]
    assert result.progress.fetched_sitemaps == 2
    assert result.progress.max_depth_skips == 1


@pytest.mark.asyncio
async def test_detects_circular_references(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = {
        "https://example.com/root.xml": _fetch_result(
            "https://example.com/root.xml",
            """
            <sitemapindex>
                <sitemap><loc>https://example.com/child.xml</loc></sitemap>
            </sitemapindex>
            """,
        ),
        "https://example.com/child.xml": _fetch_result(
            "https://example.com/child.xml",
            """
            <sitemapindex>
                <sitemap><loc>https://example.com/root.xml</loc></sitemap>
            </sitemapindex>
            """,
        ),
    }

    async def fake_fetch_sitemap(url: str) -> SitemapFetchResult:
        return responses[url]

    monkeypatch.setattr(
        "seo_indexing_tracker.services.sitemap_index_parser.fetch_sitemap",
        fake_fetch_sitemap,
    )

    result = await parse_sitemap_index("https://example.com/root.xml")

    assert result.progress.circular_references == 1
    assert (
        result.circular_references[0].source_index_url
        == "https://example.com/child.xml"
    )
    assert result.circular_references[0].target_url == "https://example.com/root.xml"


@pytest.mark.asyncio
async def test_raises_if_root_is_not_sitemap_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_sitemap(url: str) -> SitemapFetchResult:
        return _fetch_result(
            url,
            "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'></urlset>",
        )

    monkeypatch.setattr(
        "seo_indexing_tracker.services.sitemap_index_parser.fetch_sitemap",
        fake_fetch_sitemap,
    )

    with pytest.raises(SitemapIndexRootTypeError, match="expected INDEX"):
        await parse_sitemap_index("https://example.com/root.xml")
