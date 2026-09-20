"""A correlation id per request: in the response header and in that request's log lines."""

import re
import secrets
from contextvars import ContextVar

HEADER = b"x-request-id"
_ACCEPTED = re.compile(r"[A-Za-z0-9._-]{1,64}")
_current: ContextVar[str] = ContextVar("request_id", default="")


def current_request_id() -> str:
    """The id of the request being served, also inside tasks it started."""
    return _current.get()


def _from_client(headers) -> str:
    for name, value in headers:
        if name.lower() == HEADER:
            sent = value.decode("latin-1")
            # A client value ends up in logs and in a response header: accept only a plain token.
            return sent if _ACCEPTED.fullmatch(sent) else ""
    return ""


class RequestId:
    """Outermost middleware, so a rejected request is traceable too."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = _from_client(scope["headers"]) or secrets.token_hex(8)
        token = _current.set(request_id)

        async def tagged(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((HEADER, request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, tagged)
        finally:
            _current.reset(token)
