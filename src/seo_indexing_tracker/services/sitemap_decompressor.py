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
