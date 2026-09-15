"""Enforce download limits before allocating decompressed response bodies."""

import zlib

from .results import CrawlError


async def read_body(response, limit: int) -> tuple[bytes, bool]:
    # An injected transport may hand us an already-consumed response.
    if response.is_stream_consumed:
        return response.content[:limit], len(response.content) > limit
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"identity", "", "gzip", "deflate"}:
        raise CrawlError("Unsupported HTTP content encoding")
    decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding in {"gzip", "deflate"} else None
    data = bytearray()
    wire_size = 0
    # Permit compression headers even for tiny output limits, but never an unbounded stream.
    wire_limit = 2 * limit + 65536
    async for chunk in response.aiter_raw(chunk_size=65536):
        wire_remaining = wire_limit - wire_size
        wire_truncated = len(chunk) > wire_remaining
        chunk = chunk[:wire_remaining]
        wire_size += len(chunk)
        while chunk:
            if decoder and decoder.eof:
                if encoding != "gzip":
                    raise CrawlError("Unexpected trailing compressed response data")
                decoder = zlib.decompressobj(31)  # gzip permits concatenated members.
            available = limit - len(data)
            try:
                decoded = decoder.decompress(chunk, max_length=available + 1) if decoder else chunk
            except zlib.error as exc:
                raise CrawlError("Invalid compressed HTTP response") from exc
            data.extend(decoded[:available])
            if len(decoded) > available:
                return bytes(data), True
            chunk = decoder.unused_data if decoder else b""
        if wire_truncated:
            return bytes(data), True
    if decoder and not decoder.eof:
        raise CrawlError("Incomplete compressed HTTP response")
    return bytes(data), False
