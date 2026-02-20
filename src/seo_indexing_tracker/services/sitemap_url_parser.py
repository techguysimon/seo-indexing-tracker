"""Streaming URL-set sitemap parsing utilities."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import logging
from typing import Generator
from urllib.parse import urlsplit

from lxml import etree  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


class SitemapURLParserError(Exception):
    """Base exception for URL-set sitemap parsing failures."""


class SitemapURLXMLParseError(SitemapURLParserError):
    """Raised when URL-set sitemap XML cannot be parsed."""


@dataclass(slots=True, frozen=True)
class SitemapURLRecord:
    """Parsed URL entry from a sitemap URL set."""

    url: str
    lastmod: str | None
    changefreq: str | None
    priority: float | None


def _normalize_tag_name(tag_name: str) -> str:
    if tag_name.startswith("{"):
        _, _, local_name = tag_name.partition("}")
        return local_name.lower()

    _, _, local_name = tag_name.rpartition(":")
    if local_name:
        return local_name.lower()

    return tag_name.lower()


def _to_xml_bytes(xml_content: bytes | str) -> bytes:
    if isinstance(xml_content, bytes):
        xml_bytes = xml_content
    else:
        xml_bytes = xml_content.encode("utf-8")

    if not xml_bytes.strip():
        raise SitemapURLXMLParseError("Sitemap URL set XML content is empty")

    return xml_bytes


def _is_valid_http_url(url: str) -> bool:
    parsed_url = urlsplit(url)
    return parsed_url.scheme in {"http", "https"} and bool(parsed_url.netloc)


def _parse_priority(priority_value: str | None, *, url: str) -> float | None:
    if priority_value is None:
        return None

    try:
        return float(priority_value)
    except ValueError:
        logger.warning(
            "Skipping invalid sitemap priority %r for URL %r",
            priority_value,
            url,
        )
        return None


def _release_element_memory(element: etree._Element) -> None:
    element.clear()
    parent = element.getparent()
    if parent is None:
        return

    while element.getprevious() is not None:
        del parent[0]


def parse_sitemap_urls_stream(
    xml_content: bytes | str,
) -> Generator[SitemapURLRecord, None, None]:
    """Stream-parse URL-set sitemap XML and yield URL records."""

    xml_bytes = _to_xml_bytes(xml_content)
    xml_stream = BytesIO(xml_bytes)

    try:
        context = etree.iterparse(
            xml_stream,
            events=("end",),
            resolve_entities=False,
            no_network=True,
            recover=False,
        )

        for _, element in context:
            if not isinstance(element.tag, str):
                continue

            if _normalize_tag_name(element.tag) != "url":
                continue

            loc: str | None = None
            lastmod: str | None = None
            changefreq: str | None = None
            priority_text: str | None = None

            for child in element:
                if not isinstance(child.tag, str):
                    continue

                tag_name = _normalize_tag_name(child.tag)
                child_text = child.text.strip() if child.text else None
                if child_text is None:
                    continue

                if tag_name == "loc":
                    loc = child_text
                    continue

                if tag_name == "lastmod":
                    lastmod = child_text
                    continue

                if tag_name == "changefreq":
                    changefreq = child_text
                    continue

                if tag_name == "priority":
                    priority_text = child_text

            if not loc:
                logger.warning("Skipping sitemap URL entry without <loc> value")
                _release_element_memory(element)
                continue

            if not _is_valid_http_url(loc):
                logger.warning("Skipping malformed sitemap URL %r", loc)
                _release_element_memory(element)
                continue

            yield SitemapURLRecord(
                url=loc,
                lastmod=lastmod,
                changefreq=changefreq,
                priority=_parse_priority(priority_text, url=loc),
            )
            _release_element_memory(element)
    except etree.XMLSyntaxError as exc:
        raise SitemapURLXMLParseError(f"Invalid sitemap URL set XML: {exc}") from exc


__all__ = [
    "SitemapURLParserError",
    "SitemapURLRecord",
    "SitemapURLXMLParseError",
    "parse_sitemap_urls_stream",
]
