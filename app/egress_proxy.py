"""Local HTTP/CONNECT egress guard shared by HTTPX and Selenium.

Resolve once per connection, reject non-public DNS answers, then dial an IP.
TLS remains end-to-end, so certificate checks and SNI stay with the client.
"""

import asyncio
import base64
import ssl
from contextlib import suppress
from urllib.parse import unquote, urlsplit

from .results import CrawlError
from .security import Target, resolve_target

_SHUTDOWN_WAIT_SECONDS = 2


def authority(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def host_header(target: Target) -> str:
    """Host of a forwarded plain-HTTP request: port 80 goes unnamed, as clients send it. Servers
    build absolute redirects from Host; app.fobizz.com turned "app.fobizz.com:80" into
    https://app.fobizz.com:80/gallery, a TLS handshake on port 80."""
    if target.port == 80:
        return f"[{target.host}]" if ":" in target.host else target.host
    return authority(target.host, target.port)


async def dial_target(target: Target, **kwargs):
    last_error = None
    for address in target.addresses:
        try:
            return await asyncio.open_connection(address, target.port, **kwargs)
        except OSError as exc:
            last_error = exc
    raise CrawlError("Destination connection failed") from last_error


async def resolve_proxy(proxy: str, protection: bool) -> Target:
    # Credentials go out as Proxy-Authorization; only the proxy's own address is checked.
    parsed = urlsplit(proxy)
    proxy_url = (
        f"{parsed.scheme}://{authority(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))}"
    )
    try:
        return await asyncio.to_thread(resolve_target, proxy_url, protection)
    except CrawlError as exc:
        raise CrawlError(f"Proxy rejected: {exc}", 400) from exc


async def dial_upstream(target: Target, proxy: str, protection: bool):
    parsed = urlsplit(proxy)
    proxy_target = await resolve_proxy(proxy, protection)
    tls = (
        {"ssl": ssl.create_default_context(), "server_hostname": proxy_target.host} if parsed.scheme == "https" else {}
    )
    reader, writer = await dial_target(proxy_target, **tls)
    try:
        # The upstream proxy must not resolve the original destination again.
        destination = authority(target.addresses[0], target.port)
        headers = f"CONNECT {destination} HTTP/1.1\r\nHost: {destination}\r\n"
        if parsed.username is not None:
            credentials = unquote(parsed.username) + ":" + unquote(parsed.password or "")
            headers += "Proxy-Authorization: Basic " + base64.b64encode(credentials.encode()).decode() + "\r\n"
        writer.write((headers + "\r\n").encode("ascii"))
        await writer.drain()
        response = await reader.readuntil(b"\r\n\r\n")
        if response.split(b" ", 2)[1:2] != [b"200"]:  # a status line without a status is a rejection
            raise CrawlError("Upstream proxy rejected the connection")
        return reader, writer
    except BaseException:
        writer.close()
        raise


async def _copy(reader, writer):
    while chunk := await reader.read(65536):
        writer.write(chunk)
        await writer.drain()


async def _relay(client_reader, client_writer, remote_reader, remote_writer):
    tasks = [
        asyncio.create_task(_copy(client_reader, remote_writer)),
        asyncio.create_task(_copy(remote_reader, client_writer)),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            if task.done():
                task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class EgressProxy:
    def __init__(self, *, protection: bool = True, upstream: str | None = None, max_connections: int = 64):
        self.protection = protection
        self.upstream = upstream
        self.max_connections = max_connections
        self.server = None
        self.tasks = set()
        self.blocked = 0

    @property
    def port(self):
        return self.server.sockets[0].getsockname()[1]

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    async def __aenter__(self):
        if self.upstream:
            # An unusable proxy is the caller's 400 before any connection; every tunnel checks it again.
            await resolve_proxy(self.upstream, self.protection)
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=65536)
        return self

    def _accept(self, reader, writer):
        if len(self.tasks) >= self.max_connections:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def __aexit__(self, *args):
        self.server.close()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # All handlers are finished. wait_closed() also waits for each transport to detach,
        # which CPython's Windows proactor can skip after a connection reset (killed browser):
        # an unbounded wait would hang the request past its deadline and leak its capacity slot.
        with suppress(TimeoutError):
            await asyncio.wait_for(self.server.wait_closed(), _SHUTDOWN_WAIT_SECONDS)

    async def _handle(self, reader, writer):
        remote = None
        established = False
        try:
            async with asyncio.timeout(600):
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
                lines = head.decode("latin-1").split("\r\n")
                method, raw_url, version = lines[0].split(" ")
                if version not in {"HTTP/1.0", "HTTP/1.1"}:
                    raise CrawlError("Invalid proxy request", 400)
                connect = method == "CONNECT"
                target_url = "https://" + raw_url if connect else raw_url
                target = await asyncio.to_thread(resolve_target, target_url, self.protection)
                remote_reader, remote = (
                    await dial_upstream(target, self.upstream, self.protection)
                    if self.upstream
                    else await dial_target(target)
                )
                if connect:
                    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    await writer.drain()
                else:
                    parsed = urlsplit(raw_url)
                    if parsed.scheme != "http":
                        raise CrawlError("HTTPS requires CONNECT", 400)
                    path = parsed.path or "/"
                    if parsed.query:
                        path += "?" + parsed.query
                    # Pin both the connection and HTTP Host, ignoring any client mismatch.
                    headers = [
                        line
                        for line in lines[1:]
                        if line
                        and line.split(":", 1)[0].lower()
                        not in {"host", "proxy-authorization", "proxy-connection", "connection"}
                    ]
                    headers += [f"Host: {host_header(target)}", "Connection: close"]
                    remote.write(
                        (f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\n\r\n").encode("latin-1")
                    )
                    await remote.drain()
                established = True
                await _relay(reader, writer, remote_reader, remote)
        except CrawlError as exc:
            if not established:
                status = 403 if exc.status_code == 400 else 502
                self.blocked += status == 403
                writer.write(
                    f"HTTP/1.1 {status} Egress rejected\r\nContent-Length: 0\r\nConnection: close\r\nX-Extraction-Policy: blocked\r\n\r\n".encode()
                )
                with suppress(OSError):
                    await writer.drain()
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            # A malformed/disconnected/overdue connection is closed, never retried without the guard.
            if not established:
                writer.write(b"HTTP/1.1 502 Egress connection failed\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        finally:
            if remote:
                remote.close()
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
