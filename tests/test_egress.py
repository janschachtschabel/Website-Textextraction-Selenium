import asyncio
import base64

import httpx
import pytest

from app.egress_proxy import EgressProxy, dial_target
from app.results import CrawlError
from app.security import Target


async def test_guard_rejects_private_targets_even_for_connect():
    async with EgressProxy() as guard:
        reader, writer = await asyncio.open_connection("127.0.0.1", guard.port)
        writer.write(b"CONNECT 127.0.0.1:80 HTTP/1.1\r\nHost: 127.0.0.1:80\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        assert response.startswith(b"HTTP/1.1 403")
        writer.close()
        await writer.wait_closed()


async def test_dial_uses_numeric_address_not_unchecked_hostname(monkeypatch):
    calls = []

    async def connect(host, port, **kwargs):
        calls.append((host, port))
        return object(), object()

    monkeypatch.setattr(asyncio, "open_connection", connect)
    await dial_target(Target("rebind.example", 443, ("93.184.216.34",), "https"))
    assert calls == [("93.184.216.34", 443)]


async def test_guard_forwards_only_to_allowed_fixture_and_strips_proxy_auth():
    seen = []

    async def origin(reader, writer):
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(origin, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        async with EgressProxy(protection=False) as guard:
            async with httpx.AsyncClient(proxy=guard.url, trust_env=False) as client:
                result = await client.get(
                    f"http://127.0.0.1:{port}/page", headers={"Proxy-Authorization": "Basic SECRET"}
                )
            assert result.text == "OK"
        assert b"Proxy-Authorization" not in seen[0]
        assert b"GET /page HTTP/1.1" in seen[0]
    finally:
        server.close()
        await server.wait_closed()


async def test_upstream_proxy_receives_pinned_destination_and_its_own_auth():
    from app.egress_proxy import dial_upstream

    seen = []

    async def upstream(reader, writer):
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        _reader, writer = await dial_upstream(
            Target("remote.example", 443, ("93.184.216.34",), "https"),
            f"http://alice:password@127.0.0.1:{port}",
            protection=False,
        )
        assert seen[0].startswith(b"CONNECT 93.184.216.34:443 ")
        assert base64.b64encode(b"alice:password") in seen[0]
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    ("proxy", "reason"),
    [
        ("http://127.0.0.1:8767", "blocked by network policy"),
        ("https://[::1]:3128", "blocked by network policy"),
        ("http://alice:secret@internal-proxy.example:3128", "blocked by network policy"),
        ("http://missing-proxy.example:3128", "unresolvable"),
    ],
)
async def test_unusable_upstream_proxy_is_rejected_before_the_guard_starts(dns, proxy, reason):
    dns["internal-proxy.example"] = "10.0.0.7"
    with pytest.raises(CrawlError, match=f"^Proxy rejected: .*{reason}") as rejected:
        async with EgressProxy(upstream=proxy):
            pytest.fail("An unusable upstream proxy must not start a guard")
    assert rejected.value.status_code == 400


async def test_authenticated_public_proxy_and_policy_opt_out_start_the_guard(dns):
    dns["proxy.example"] = "93.184.216.34"
    for proxy, protection in [("http://alice:secret@proxy.example:3128", True), ("http://127.0.0.1:8767", False)]:
        async with EgressProxy(protection=protection, upstream=proxy) as guard:
            assert guard.url.startswith("http://127.0.0.1:")


async def test_proxy_shutdown_does_not_hang_on_a_connection_that_never_detaches():
    # On Windows, CPython's proactor can skip Server._detach() after a reset connection
    # (a killed browser), so Server.wait_closed() would never return.
    guard = await EgressProxy().__aenter__()
    never = asyncio.get_running_loop().create_future()

    async def stuck_wait_closed():
        await never

    guard.server.wait_closed = stuck_wait_closed
    await asyncio.wait_for(guard.__aexit__(), timeout=10)
    assert not guard.server.is_serving()


async def test_proxy_shutdown_cancels_open_tunnels_before_waiting_for_server():
    remote_tasks = set()

    async def origin(reader, writer):
        task = asyncio.current_task()
        remote_tasks.add(task)
        try:
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            remote_tasks.discard(task)

    server = await asyncio.start_server(origin, "127.0.0.1", 0)
    guard = await EgressProxy(protection=False).__aenter__()
    reader, writer = await asyncio.open_connection("127.0.0.1", guard.port)
    try:
        port = server.sockets[0].getsockname()[1]
        writer.write(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        await writer.drain()
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        await asyncio.wait_for(guard.__aexit__(), timeout=0.5)
        assert not guard.tasks
    finally:
        for task in list(guard.tasks):
            task.cancel()
        await asyncio.gather(*list(guard.tasks), return_exceptions=True)
        writer.close()
        await writer.wait_closed()
        server.close()
        for task in list(remote_tasks):
            task.cancel()
        await asyncio.gather(*list(remote_tasks), return_exceptions=True)
        await server.wait_closed()


@pytest.mark.parametrize("reply", [b"garbage\r\n\r\n", b"\r\n\r\n", b"HTTP/1.1\r\n\r\n"])
async def test_a_malformed_upstream_proxy_reply_is_a_crawl_error(reply):
    """Not a 502 but an IndexError, it would abort the tunnel instead of answering it."""
    from app.egress_proxy import dial_upstream

    async def upstream(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(reply)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(CrawlError, match="Upstream proxy rejected"):
            await dial_upstream(
                Target("remote.example", 443, ("93.184.216.34",), "https"),
                f"http://127.0.0.1:{port}",
                protection=False,
            )
    finally:
        server.close()
        await server.wait_closed()
