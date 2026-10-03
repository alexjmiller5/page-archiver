"""A capture-only HTTP proxy that pins every outbound connection to a public IP.

Chromium uses this proxy for navigation, resources and WebSockets. There is no
MITM, persistent listener, credential injection or request/header logging.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


def public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast and not address.is_reserved


def validate_url(value: str):
    if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("invalid capture URL")
    url = urlsplit(value)
    if url.scheme not in ("http", "https") or not url.hostname or url.username is not None:
        raise ValueError("capture requires an HTTP(S) URL without credentials")
    # Accessing port validates its numeric range and syntax.
    _ = url.port
    try:
        literal = ipaddress.ip_address(url.hostname)
    except ValueError:
        if url.hostname.lower().rstrip(".") == "localhost" or "%" in url.hostname:
            raise ValueError("nonpublic destination") from None
    else:
        if not public_ip(str(literal)):
            raise ValueError("nonpublic destination")
    return url


def public_addresses(host: str, port: int) -> list[str]:
    results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(item[4][0] for item in results))
    if not addresses or any(not public_ip(address) for address in addresses):
        raise ValueError("nonpublic destination")
    return addresses


class CaptureProxy:
    def __init__(self, max_bytes: int = 250 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.transferred = 0
        self.exhausted = False
        self.port = 0
        self._tasks = set()
        self._closing = False

    @property
    def active_connections(self) -> int:
        return len(self._tasks)

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=16384)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_):
        self._closing = True
        self.server.close()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.server.wait_closed()

    def _accept(self, reader, writer):
        if self._closing:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        # Cancellation can precede the coroutine's first instruction, so its
        # finally block alone cannot own accepted-socket cleanup.
        task.add_done_callback(lambda _: writer.close())

    async def _dial(self, host, port):
        addresses = await asyncio.to_thread(public_addresses, host, port)
        for address in addresses:
            try:
                # Never pass the hostname to the dialer: a second DNS lookup
                # could turn a checked public answer into a private connection.
                return await asyncio.wait_for(asyncio.open_connection(address, port), 5)
            except (OSError, TimeoutError):
                continue
        raise OSError("destination unavailable")

    async def _pump(self, reader, writer):
        while chunk := await asyncio.wait_for(reader.read(65536), 15):
            self.transferred += len(chunk)
            if self.transferred > self.max_bytes:
                self.exhausted = True
                raise ValueError("capture transfer limit")
            writer.write(chunk)
            await writer.drain()

    async def _handle(self, reader, writer):
        upstream = None
        relays = []
        replied = False
        status = 502
        try:
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            except asyncio.LimitOverrunError:
                status = 431
                raise ValueError("headers too large") from None
            lines = raw.decode("latin-1").split("\r\n")
            method, target, version = lines[0].split(" ")
            if version not in ("HTTP/1.0", "HTTP/1.1"):
                raise ValueError("unsupported HTTP version")
            if method == "CONNECT":
                url = validate_url("https://" + target)
                if url.path or url.query or url.fragment:
                    raise ValueError("invalid CONNECT authority")
                port = url.port or 443
            else:
                url = validate_url(target)
                if url.scheme != "http":
                    raise ValueError("HTTPS requires CONNECT")
                port = url.port or 80
            try:
                remote_reader, upstream = await self._dial(url.hostname, port)
            except ValueError:
                status = 403
                raise
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                replied = True
            else:
                path = (url.path or "/") + ("?" + url.query if url.query else "")
                headers = []
                for line in lines[1:]:
                    if not line:
                        continue
                    name, _, value = line.partition(":")
                    if not _ or name.lower() in (
                        "proxy-authorization",
                        "proxy-connection",
                        "connection",
                        "host",
                    ):
                        continue
                    headers.append(name + ":" + value)
                headers.extend(["Host: " + url.netloc, "Connection: close", "", ""])
                upstream.write(
                    (f"{method} {path} {version}\r\n" + "\r\n".join(headers)).encode("latin-1")
                )
                await upstream.drain()
                replied = True
            relays = [
                asyncio.create_task(self._pump(reader, upstream)),
                asyncio.create_task(self._pump(remote_reader, writer)),
            ]
            await asyncio.wait(relays, return_when=asyncio.FIRST_COMPLETED)
        except ValueError:
            if status != 431:
                status = 403
        except (OSError, TimeoutError, asyncio.IncompleteReadError, UnicodeError):
            pass
        finally:
            for relay in relays:
                relay.cancel()
            await asyncio.gather(*relays, return_exceptions=True)
            if not replied and not writer.is_closing():
                writer.write(
                    f"HTTP/1.1 {status} Request denied\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
                )
            writers = [item for item in (upstream, writer) if item is not None]
            for stream in writers:
                stream.close()
            await asyncio.gather(
                *(stream.wait_closed() for stream in writers), return_exceptions=True
            )
