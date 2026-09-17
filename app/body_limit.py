"""Cap request bodies before routing: FastAPI reads the whole body before authentication."""

import json

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


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
                return await self._reject(send)
            if not message.get("more_body", False):
                break
        replay = [{"type": "http.request", "body": body, "more_body": False}, *pending]

        async def replayed():
            return replay.pop(0) if replay else await receive()

        await self.app(scope, replayed, send)

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
