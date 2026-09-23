"""Enforce download limits before allocating decompressed response bodies."""

import zlib

from .results import CrawlError

# Content codings from the IANA registry this reader cannot decode. Any other unknown token is
# read as the plain body, as browsers do: medienportal.siemens-stiftung.org sends
# "Content-Encoding: (with " over plain HTML.
_UNDECODABLE = {"aes128gcm", "br", "compress", "dcb", "dcz", "exi", "pack200-gzip", "x-compress", "zstd"}


def _deflate_wbits(head: bytes) -> int:
    """Accept raw deflate (wbits -15) as HTTPX does; some servers omit the RFC 1950 wrapper."""
    wrapped = len(head) >= 2 and head[0] & 0x0F == 8 and int.from_bytes(head[:2], "big") % 31 == 0
    return 15 if wrapped else -15


# Complexity 12, deliberately: every branch belongs to the one job of reading a bounded
# body - the encoding check, wrapped against raw deflate, the wire and output limits,
# concatenated gzip members, completeness. Splitting it would scatter state that the
# loop carries, in code whose whole point is to bound a decompression bomb.
async def read_body(response, limit: int) -> tuple[bytes, bool]:  # noqa: C901
    # An injected transport may hand us an already-consumed response.
    if response.is_stream_consumed:
        return response.content[:limit], len(response.content) > limit
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if "," in encoding or encoding in _UNDECODABLE:
        raise CrawlError("Unsupported HTTP content encoding")
    encoding = {"x-gzip": "gzip"}.get(encoding, encoding)
    compressed = encoding in {"gzip", "deflate"}
    decoder = None  # deflate needs the first bytes to tell wrapped from raw
    data = bytearray()
    wire_size = 0
    # Permit compression headers even for tiny output limits, but never an unbounded stream.
    wire_limit = 2 * limit + 65536
    async for chunk in response.aiter_raw(chunk_size=65536):
        if compressed and decoder is None and chunk:
            decoder = zlib.decompressobj(31 if encoding == "gzip" else _deflate_wbits(chunk))
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
    if compressed and (decoder is None or not decoder.eof):
        raise CrawlError("Incomplete compressed HTTP response")
    return bytes(data), False
