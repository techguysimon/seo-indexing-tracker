"""Detect sitemap resource type from XML payloads."""

from __future__ import annotations

from lxml import etree  # type: ignore[import-untyped]

from seo_indexing_tracker.models.sitemap import SitemapType


class SitemapTypeDetectionError(Exception):
    """Base exception raised for sitemap type detection failures."""


class SitemapXMLParseError(SitemapTypeDetectionError):
    """Raised when sitemap XML cannot be parsed."""


class UnknownSitemapTypeError(SitemapTypeDetectionError):
    """Raised when sitemap XML root element is unsupported."""


def _normalize_root_element_name(tag_name: str) -> str:
    if tag_name.startswith("{"):
        _, _, local_name = tag_name.partition("}")
        return local_name.lower()

    _, _, local_name = tag_name.rpartition(":")
    if local_name:
        return local_name.lower()

    return tag_name.lower()


def detect_sitemap_type(xml_content: str | bytes) -> SitemapType:
    """Parse XML and return sitemap type based on root element."""

    if isinstance(xml_content, str):
        xml_bytes = xml_content.encode("utf-8")
    else:
        xml_bytes = xml_content

    if not xml_bytes.strip():
        raise SitemapXMLParseError("Sitemap XML content is empty")

    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)

    try:
        root = etree.fromstring(xml_bytes, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise SitemapXMLParseError(f"Invalid sitemap XML: {exc}") from exc

    if not isinstance(root.tag, str):
        raise UnknownSitemapTypeError(
            "Sitemap XML root element is not a valid XML element"
        )

    root_name = _normalize_root_element_name(root.tag)

    if root_name == "sitemapindex":
        return SitemapType.INDEX

    if root_name == "urlset":
        return SitemapType.URLSET

    raise UnknownSitemapTypeError(
        f"Unsupported sitemap root element <{root_name}>. Expected <sitemapindex> or <urlset>."
    )


__all__ = [
    "SitemapTypeDetectionError",
    "SitemapXMLParseError",
    "UnknownSitemapTypeError",
    "detect_sitemap_type",
]
