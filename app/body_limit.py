"""Cap request bodies before routing: FastAPI reads the whole body before authentication."""

import asyncio
import json
from contextlib import suppress

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
DRAIN_BYTES = 4 * 1024 * 1024  # of a rejected upload, so the client can read the answer
DRAIN_SECONDS = 1.0


def _declared_length(headers) -> int | None:
    for name, value in headers:
        if name.lower() == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class BodySizeLimit:
    """ASGI middleware answering 413 for bodies above ``max_bytes``; never buffers more than that."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in _BODY_METHODS:
            return await self.app(scope, receive, send)
        declared = _declared_length(scope["headers"])
        if declared is not None and declared > self.max_bytes:
            await self._drain(receive)
            return await self._reject(send)
        body = b""
        pending = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                pending.append(message)  # http.disconnect
                break
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                await self._drain(receive)
                return await self._reject(send)
            if not message.get("more_body", False):
                break
        replay = [{"type": "http.request", "body": body, "more_body": False}, *pending]

        async def replayed():
            return replay.pop(0) if replay else await receive()

        await self.app(scope, replayed, send)

    async def _drain(self, receive):
        """Read a bounded rest of a rejected upload: a client still sending sees a reset, not the 413."""
        discarded = 0
        with suppress(TimeoutError):
            async with asyncio.timeout(DRAIN_SECONDS):
                while discarded < DRAIN_BYTES:
                    message = await receive()
                    if message["type"] != "http.request" or not message.get("more_body", False):
                        return
                    discarded += len(message.get("body", b""))

    async def _reject(self, send):
        payload = json.dumps({"detail": f"Request body exceeds {self.max_bytes} bytes"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
