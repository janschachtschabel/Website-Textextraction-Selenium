import asyncio
import base64

import httpx
import pytest

from app.egress_proxy import EgressProxy, dial_target
from app.security import Target


async def test_guard_rejects_private_targets_even_for_connect():
    async with EgressProxy() as guard:
        reader, writer = await asyncio.open_connection('127.0.0.1', guard.port)
        writer.write(b'CONNECT 127.0.0.1:80 HTTP/1.1\r\nHost: 127.0.0.1:80\r\n\r\n')
        await writer.drain()
        response = await reader.read()
        assert response.startswith(b'HTTP/1.1 403')
        writer.close()
        await writer.wait_closed()


async def test_dial_uses_numeric_address_not_unchecked_hostname(monkeypatch):
    calls = []
    async def connect(host, port, **kwargs):
        calls.append((host, port))
        return object(), object()
    monkeypatch.setattr(asyncio, 'open_connection', connect)
    await dial_target(Target('rebind.example', 443, ('93.184.216.34',), 'https'))
    assert calls == [('93.184.216.34', 443)]


async def test_guard_forwards_only_to_allowed_fixture_and_strips_proxy_auth():
    seen = []
    async def origin(reader, writer):
        seen.append(await reader.readuntil(b'\r\n\r\n'))
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(origin, '127.0.0.1', 0)
    try:
        port = server.sockets[0].getsockname()[1]
        async with EgressProxy(protection=False) as guard:
            async with httpx.AsyncClient(proxy=guard.url, trust_env=False) as client:
                result = await client.get(f'http://127.0.0.1:{port}/page', headers={'Proxy-Authorization': 'Basic SECRET'})
            assert result.text == 'OK'
        assert b'Proxy-Authorization' not in seen[0]
        assert b'GET /page HTTP/1.1' in seen[0]
    finally:
        server.close()
        await server.wait_closed()


async def test_upstream_proxy_receives_pinned_destination_and_its_own_auth():
    from app.egress_proxy import dial_upstream
    seen = []
    async def upstream(reader, writer):
        seen.append(await reader.readuntil(b'\r\n\r\n'))
        writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(upstream, '127.0.0.1', 0)
    try:
        port = server.sockets[0].getsockname()[1]
        reader, writer = await dial_upstream(Target('remote.example', 443, ('93.184.216.34',), 'https'),
                                             f'http://alice:password@127.0.0.1:{port}', protection=False)
        assert seen[0].startswith(b'CONNECT 93.184.216.34:443 ')
        assert base64.b64encode(b'alice:password') in seen[0]
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
