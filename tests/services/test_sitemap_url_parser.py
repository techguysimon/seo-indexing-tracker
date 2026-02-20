"""Tests for streaming sitemap URL parsing."""

from __future__ import annotations

import logging

import pytest

from seo_indexing_tracker.services.sitemap_url_parser import (
    SitemapURLRecord,
    SitemapURLXMLParseError,
    parse_sitemap_urls_stream,
)


def test_stream_parser_extracts_standard_url_fields() -> None:
    xml_content = b"""
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url>
            <loc>https://example.com/alpha</loc>
            <lastmod>2026-02-19</lastmod>
            <changefreq>daily</changefreq>
            <priority>0.8</priority>
        </url>
        <url>
            <loc>https://example.com/beta</loc>
        </url>
    </urlset>
    """

    records = list(parse_sitemap_urls_stream(xml_content))

    assert records == [
        SitemapURLRecord(
            url="https://example.com/alpha",
            lastmod="2026-02-19",
            changefreq="daily",
            priority=0.8,
        ),
        SitemapURLRecord(
            url="https://example.com/beta",
            lastmod=None,
            changefreq=None,
            priority=None,
        ),
    ]


def test_stream_parser_handles_prefixed_namespace_and_extensions() -> None:
    xml_content = """
    <sm:urlset
        xmlns:sm="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1"
        xmlns:video="http://www.google.com/schemas/sitemap-video/1.1"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"
    >
        <sm:url>
            <sm:loc>https://example.com/with-extensions</sm:loc>
            <image:image>
                <image:loc>https://example.com/image.jpg</image:loc>
            </image:image>
            <video:video>
                <video:title>Example Video</video:title>
            </video:video>
            <news:news>
                <news:publication_date>2026-02-20</news:publication_date>
            </news:news>
        </sm:url>
    </sm:urlset>
    """

    records = list(parse_sitemap_urls_stream(xml_content))

    assert len(records) == 1
    assert records[0].url == "https://example.com/with-extensions"


def test_stream_parser_skips_malformed_urls_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    xml_content = """
    <urlset>
        <url><loc>://bad url</loc></url>
        <url><loc>https://example.com/good</loc></url>
    </urlset>
    """

    with caplog.at_level(logging.WARNING):
        records = list(parse_sitemap_urls_stream(xml_content))

    assert [record.url for record in records] == ["https://example.com/good"]
    assert "Skipping malformed sitemap URL" in caplog.text


def test_stream_parser_skips_missing_loc_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    xml_content = """
    <urlset>
        <url><lastmod>2026-02-20</lastmod></url>
    </urlset>
    """

    with caplog.at_level(logging.WARNING):
        records = list(parse_sitemap_urls_stream(xml_content))

    assert records == []
    assert "without <loc> value" in caplog.text


def test_stream_parser_raises_for_invalid_xml() -> None:
    with pytest.raises(SitemapURLXMLParseError, match="Invalid sitemap URL set XML"):
        list(parse_sitemap_urls_stream("<urlset><url></urlset>"))
