"""Utilities for detecting and decompressing gzipped sitemap payloads."""

from __future__ import annotations

import gzip
import zlib
from collections.abc import Iterable, Iterator
from typing import Final
from urllib.parse import urlsplit

GZIP_FILE_SUFFIX: Final[str] = ".xml.gz"
GZIP_CONTENT_ENCODING: Final[str] = "gzip"
_GZIP_WBITS: Final[int] = 16 + zlib.MAX_WBITS
_GZIP_MAGIC_BYTES: Final[bytes] = b"\x1f\x8b"
_UTF8_BOM_BYTES: Final[bytes] = b"\xef\xbb\xbf"


class SitemapDecompressionError(Exception):
    """Raised when sitemap gzip decompression fails."""


def _has_gzip_extension(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return path.endswith(GZIP_FILE_SUFFIX)


def _header_contains_gzip(content_encoding: str) -> bool:
    encoding_tokens = {
        token.strip().lower() for token in content_encoding.split(",") if token.strip()
    }
    return GZIP_CONTENT_ENCODING in encoding_tokens


def has_gzip_magic_bytes(content: bytes) -> bool:
    """Return True when bytes begin with the gzip magic header."""

    return len(content) >= len(_GZIP_MAGIC_BYTES) and content.startswith(
        _GZIP_MAGIC_BYTES
    )


def is_probably_xml_content(content: bytes) -> bool:
    """Return True when payload appears to already be XML text."""

    if not content:
        return False

    stripped = content.lstrip()
    if stripped.startswith(_UTF8_BOM_BYTES):
        stripped = stripped[len(_UTF8_BOM_BYTES) :].lstrip()

    return stripped.startswith(b"<")


def is_gzipped_sitemap(
    *, url: str | None = None, content_encoding: str | None = None
) -> bool:
    """Return True when sitemap payload should be treated as gzip compressed."""

    if content_encoding and _header_contains_gzip(content_encoding):
        return True

    if url and _has_gzip_extension(url):
        return True

    return False


def decompress_gzipped_content(content: bytes) -> bytes:
    """Decompress gzipped sitemap content buffered in memory."""

    if not content:
        return content

    try:
        return gzip.decompress(content)
    except (OSError, EOFError, zlib.error) as exc:
        raise SitemapDecompressionError(
            "Failed to decompress gzipped sitemap bytes"
        ) from exc


def decompress_gzipped_stream(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Yield decompressed bytes from a gzip-compressed chunk stream."""

    decompressor = zlib.decompressobj(_GZIP_WBITS)

    try:
        for chunk in chunks:
            if not chunk:
                continue

            yield decompressor.decompress(chunk)

        flush_data = decompressor.flush()
        if flush_data:
            yield flush_data
    except zlib.error as exc:
        raise SitemapDecompressionError(
            "Failed to decompress gzipped sitemap stream"
        ) from exc
