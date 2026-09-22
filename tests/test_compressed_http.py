import gzip
import tracemalloc

import httpx

from app.deadline import Deadline
from app.http_fetcher import HTTPFetcher
from app.schemas import CrawlRequest, resolve_options


class GzipStream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


async def test_compressed_response_is_bounded_before_allocating_decoded_body():
    compressed = gzip.compress(b"x" * (16 * 1024 * 1024))
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, stream=GzipStream(compressed), headers={"content-type": "text/html", "content-encoding": "gzip"}
        )
    )
    fetcher = HTTPFetcher(transport=transport, validate=lambda url: None)
    options = resolve_options(CrawlRequest(url="https://example.com", max_bytes=1024))
    tracemalloc.start()
    try:
        result = await fetcher.fetch("https://example.com", options, Deadline(5))
        _, peak = tracemalloc.get_traced_memory()
        assert len(result.data) == 1024 and result.truncated
        assert peak < 8 * 1024 * 1024
    finally:
        tracemalloc.stop()
        await fetcher.close()


async def test_uncompressed_gzip_framing_does_not_truncate_exact_output_limit():
    import zlib

    from app.body_reader import read_body

    payload = b"x" * 65536
    for encoding, compressed in [
        ("gzip", gzip.compress(payload, compresslevel=0)),
        ("deflate", zlib.compress(payload, level=0)),
    ]:
        response = httpx.Response(200, stream=GzipStream(compressed), headers={"content-encoding": encoding})
        body, truncated = await read_body(response, len(payload))
        assert body == payload and not truncated


async def test_concatenated_gzip_members_respect_total_output_limit():
    from app.body_reader import read_body

    compressed = gzip.compress(b"hello ") + gzip.compress(b"world")
    for limit, expected, shortened in [(11, b"hello world", False), (8, b"hello wo", True)]:
        response = httpx.Response(200, stream=GzipStream(compressed), headers={"content-encoding": "gzip"})
        body, truncated = await read_body(response, limit)
        assert body == expected and truncated is shortened


async def test_raw_deflate_bodies_are_decoded_like_zlib_wrapped_ones():
    import zlib

    from app.body_reader import read_body

    payload = b"<html><body>Raw deflate</body></html>"
    raw = zlib.compressobj(wbits=-15)
    compressed = raw.compress(payload) + raw.flush()
    response = httpx.Response(200, stream=GzipStream(compressed), headers={"content-encoding": "deflate"})
    assert await read_body(response, 4096) == (payload, False)


async def test_corrupt_deflate_bodies_are_still_rejected():
    import pytest

    from app.body_reader import read_body
    from app.results import CrawlError

    response = httpx.Response(200, stream=GzipStream(b"not compressed at all"), headers={"content-encoding": "deflate"})
    with pytest.raises(CrawlError, match="compressed"):
        await read_body(response, 4096)


class NoBody(httpx.AsyncByteStream):
    """A 304 carries no body at all - not an empty chunk, no chunk."""

    async def __aiter__(self):
        for chunk in ():
            yield chunk


async def test_a_304_that_echoes_content_encoding_has_no_body_to_decompress():
    """A 304 keeps the headers of the representation it stands for, Content-Encoding among
    them - Wikimedia, Fastly and Cloudflare all do this. Reading it as a gzip stream found
    no end of one and failed the crawl with 502 "Incomplete compressed HTTP response", for
    every repeat crawl until the stale entry expired a day later."""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(304, stream=NoBody(), headers={"content-encoding": "gzip"})
    )
    fetcher = HTTPFetcher(transport=transport, validate=lambda url: None)
    try:
        result = await fetcher.fetch(
            "https://example.com", resolve_options(CrawlRequest(url="https://example.com")), Deadline(5)
        )
        assert result.status_code == 304 and result.data == b"" and not result.truncated
    finally:
        await fetcher.close()
