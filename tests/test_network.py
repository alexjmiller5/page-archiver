import asyncio
import socket

import pytest

from page_archiver.network import CaptureProxy, public_addresses, validate_url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a",
        "https://user:secret@example.com",
        "https://example.com/\r\nX: y",
        "http://[::1]/",
    ],
)
def test_invalid_or_local_urls_rejected(url):
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.169.254",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "224.0.0.1",
        "0.0.0.0",
    ],
)
def test_private_addresses_and_mapped_forms_rejected(monkeypatch, ip):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
        ],
    )
    with pytest.raises(ValueError):
        public_addresses("public-looking.example", 443)


def test_mixed_dns_answers_rejected_not_filtered(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
            for ip in ["93.184.216.34", "10.0.0.1"]
        ],
    )
    with pytest.raises(ValueError):
        public_addresses("rebind.example", 443)


def test_public_dns_results_are_unique_and_returned_as_literal_ips(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))] * 2,
    )
    assert public_addresses("page.example", 443) == ["93.184.216.34"]


def test_proxy_dials_validated_address_and_preserves_tls_tunnel(monkeypatch):
    async def scenario():
        async def upstream(reader, writer):
            writer.write(await reader.read(100))
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        # Test-only DNS seam: production resolver never permits loopback.
        monkeypatch.setattr(
            "page_archiver.network.public_addresses", lambda host, port: ["127.0.0.1"]
        )
        try:
            async with CaptureProxy() as proxy:
                reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
                writer.write(
                    f"CONNECT public.example:{port} HTTP/1.1\r\nHost: public.example\r\n\r\n".encode()
                )
                await writer.drain()
                assert (
                    await reader.readuntil(b"\r\n\r\n")
                    == b"HTTP/1.1 200 Connection Established\r\n\r\n"
                )
                writer.write(b"opaque TLS bytes")
                await writer.drain()
                assert await reader.read() == b"opaque TLS bytes"
                writer.close()
                await writer.wait_closed()
            assert proxy.active_connections == 0
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_proxy_rejects_private_connect_without_leaking_destination():
    async def scenario():
        async with CaptureProxy() as proxy:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(b"CONNECT 169.254.169.254:80 HTTP/1.1\r\n\r\n")
            await writer.drain()
            response = await reader.read()
            assert b"403" in response and b"169.254" not in response
            writer.close()
            await writer.wait_closed()

    asyncio.run(scenario())


def test_proxy_bounds_headers_and_cleans_up_open_connections():
    async def scenario():
        async with CaptureProxy() as proxy:
            port = proxy.port
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET http://example.com/ HTTP/1.1\r\nX: " + b"x" * 20000)
            await writer.drain()
            assert b"431" in await reader.read()
            writer.close()
            await writer.wait_closed()
            _, idle = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.sleep(0)
        idle.close()
        await idle.wait_closed()
        assert proxy.active_connections == 0
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", port)

    asyncio.run(scenario())


def test_http_forwarding_strips_proxy_credentials_and_bounds_transfer(monkeypatch):
    async def scenario():
        observed = []

        async def upstream(reader, writer):
            observed.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1024\r\n\r\n" + b"x" * 1024)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr("page_archiver.network.public_addresses", lambda *a: ["127.0.0.1"])
        try:
            async with CaptureProxy(max_bytes=512) as proxy:
                reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
                writer.write(
                    f"GET http://public.example:{port}/path?q=1 HTTP/1.1\r\nProxy-Authorization: secret\r\nHost: wrong.example\r\n\r\n".encode()
                )
                await writer.drain()
                assert await reader.read() == b""
                assert proxy.exhausted
                assert b"secret" not in observed[0]
                assert observed[0].startswith(b"GET /path?q=1 HTTP/1.1\r\n")
                assert f"Host: public.example:{port}".encode() in observed[0]
                writer.close()
                await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())
