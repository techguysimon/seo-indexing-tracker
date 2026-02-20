"""Tests for sitemap XML root type detection."""

from __future__ import annotations

import pytest

from seo_indexing_tracker.models.sitemap import SitemapType
from seo_indexing_tracker.services.sitemap_type_detector import (
    SitemapXMLParseError,
    UnknownSitemapTypeError,
    detect_sitemap_type,
)


def test_detects_index_type_without_namespace() -> None:
    xml = "<?xml version='1.0' encoding='UTF-8'?><sitemapindex></sitemapindex>"

    result = detect_sitemap_type(xml)

    assert result is SitemapType.INDEX


def test_detects_urlset_type_with_standard_namespace() -> None:
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'></urlset>"
    )

    result = detect_sitemap_type(xml)

    assert result is SitemapType.URLSET


def test_detects_urlset_type_with_prefixed_namespace() -> None:
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<sm:urlset xmlns:sm='http://www.sitemaps.org/schemas/sitemap/0.9'></sm:urlset>"
    )

    result = detect_sitemap_type(xml)

    assert result is SitemapType.URLSET


def test_raises_clear_error_for_malformed_xml() -> None:
    malformed_xml = "<urlset><url></urlset>"

    with pytest.raises(SitemapXMLParseError, match="Invalid sitemap XML"):
        detect_sitemap_type(malformed_xml)


def test_raises_clear_error_for_unknown_root_element() -> None:
    xml = "<feed></feed>"

    with pytest.raises(
        UnknownSitemapTypeError,
        match="Unsupported sitemap root element <feed>",
    ):
        detect_sitemap_type(xml)


def test_raises_clear_error_for_empty_content() -> None:
    with pytest.raises(SitemapXMLParseError, match="content is empty"):
        detect_sitemap_type("   ")
