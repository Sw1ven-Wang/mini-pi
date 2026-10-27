"""Small HTTP(S) allowlist proxy used only by ContainerSandbox sidecars."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
import socket
import sys
from urllib.parse import urlsplit


class AllowlistProxy:
    def __init__(self, domains: list[str]) -> None:
        self.domains = [item.lower().rstrip(".") for item in domains]

    def allowed(self, host: str) -> bool:
        candidate = host.lower().rstrip(".")
        try:
            ipaddress.ip_address(candidate)
            return False
        except ValueError:
            pass
        return any(
            candidate == pattern
            or (
                pattern.startswith("*.")
                and candidate.endswith(pattern[1:])
                and candidate != pattern[2:]
            )
            for pattern in self.domains
        )

    async def _public_endpoint(self, host: str, port: int) -> tuple[int, str] | None:
        if not self.allowed(host) or port not in {80, 443}:
            return None
        loop = asyncio.get_running_loop()
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=5,
        )
        if not infos:
            return None
        endpoints: list[tuple[int, str]] = []
        for info in infos:
            family, address_text = info[0], info[4][0]
            address = ipaddress.ip_address(address_text)
            if not address.is_global:
                return None
            endpoints.append((family, str(address)))
        # Connect to the already-validated address, not the hostname, to avoid
        # a second DNS lookup and DNS-rebinding TOCTOU.
        return endpoints[0]

    async def _connect_public(
        self,
        host: str,
        port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        endpoint = await self._public_endpoint(host, port)
        if endpoint is None:
            return None
        return await self._connect_endpoint(endpoint, port)

    @staticmethod
    async def _connect_endpoint(
        endpoint: tuple[int, str],
        port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        family, address = endpoint
        return await asyncio.wait_for(
            asyncio.open_connection(address, port, family=family),
            timeout=10,
        )

    async def handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            first = await reader.readline()
            if not first or len(first) > 8192:
                return
            parts = first.decode("latin-1", "replace").strip().split(" ", 2)
            if len(parts) != 3:
                await self._reject(writer, 400, "Bad Request")
                return
            method, target, version = parts
            headers: list[bytes] = []
            total = 0
            while True:
                line = await reader.readline()
                total += len(line)
                if total > 65536:
                    await self._reject(writer, 431, "Headers Too Large")
                    return
                if line in {b"\r\n", b"\n", b""}:
                    break
                if not line.lower().startswith((
                    b"host:", b"proxy-authorization:", b"proxy-connection:",
                )):
                    headers.append(line)

            if method.upper() == "CONNECT":
                host, separator, port_text = target.rpartition(":")
                port = int(port_text) if separator and port_text.isdigit() else 443
                if not host:
                    host = target
                endpoint = await self._public_endpoint(host, port)
                if endpoint is None:
                    await self._reject(writer, 403, "Forbidden")
                    return
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                initial_tls = await self._read_https_client_hello(reader, host)
                if initial_tls is None:
                    self._record_block(403, "Non-HTTPS CONNECT")
                    return
                upstream_reader, upstream_writer = await self._connect_endpoint(
                    endpoint, port,
                )
                upstream_writer.write(initial_tls)
                await upstream_writer.drain()
                await asyncio.gather(
                    self._pipe(reader, upstream_writer),
                    self._pipe(upstream_reader, writer),
                )
                return

            parsed = urlsplit(target)
            if parsed.scheme.lower() != "http" or not parsed.hostname:
                await self._reject(writer, 400, "Absolute HTTP URL Required")
                return
            port = parsed.port or 80
            upstream = await self._connect_public(parsed.hostname, port)
            if upstream is None:
                await self._reject(writer, 403, "Forbidden")
                return
            upstream_reader, upstream_writer = upstream
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            host_header = parsed.hostname
            if port != 80:
                host_header += f":{port}"
            upstream_writer.write(
                f"{method} {path} {version}\r\n".encode("latin-1")
                + f"Host: {host_header}\r\n".encode("latin-1")
                + b"".join(headers)
                + b"\r\n"
            )
            await upstream_writer.drain()
            await self._pipe(upstream_reader, writer)
        except Exception:
            if not writer.is_closing():
                await self._reject(writer, 502, "Bad Gateway")
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
            writer.close()
            await writer.wait_closed()

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            if not writer.is_closing():
                try:
                    writer.write_eof()
                except (OSError, RuntimeError):
                    pass

    @staticmethod
    async def _read_https_client_hello(
        reader: asyncio.StreamReader,
        expected_host: str,
    ) -> bytes | None:
        try:
            header = await asyncio.wait_for(reader.readexactly(5), timeout=5)
            if header[0] != 0x16 or header[1] != 0x03:
                return None
            length = int.from_bytes(header[3:5], "big")
            if length <= 0 or length > 65535:
                return None
            body = await asyncio.wait_for(reader.readexactly(length), timeout=5)
            server_name = _client_hello_server_name(body)
            if server_name != expected_host.lower().rstrip("."):
                return None
            return header + body
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError):
            return None

    @staticmethod
    async def _reject(
        writer: asyncio.StreamWriter,
        code: int,
        reason: str,
    ) -> None:
        AllowlistProxy._record_block(code, reason)
        body = f"{code} {reason}\n".encode("ascii")
        writer.write(
            f"HTTP/1.1 {code} {reason}\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n".encode("ascii") + body
        )
        await writer.drain()

    @staticmethod
    def _record_block(code: int, reason: str) -> None:
        print(
            f"MINI_PI_NETWORK_BLOCK code={code} reason={reason}",
            file=sys.stdout,
            flush=True,
        )


def _client_hello_server_name(record_body: bytes) -> str | None:
    """Extract an ASCII SNI from one bounded TLS ClientHello record."""
    if len(record_body) < 4 or record_body[0] != 0x01:
        return None
    handshake_length = int.from_bytes(record_body[1:4], "big")
    hello = record_body[4:4 + handshake_length]
    if len(hello) != handshake_length or len(hello) < 35:
        return None
    offset = 34  # legacy_version (2) + random (32)
    session_length = hello[offset]
    offset += 1 + session_length
    if offset + 2 > len(hello):
        return None
    cipher_length = int.from_bytes(hello[offset:offset + 2], "big")
    offset += 2 + cipher_length
    if offset >= len(hello):
        return None
    compression_length = hello[offset]
    offset += 1 + compression_length
    if offset + 2 > len(hello):
        return None
    extensions_length = int.from_bytes(hello[offset:offset + 2], "big")
    offset += 2
    extensions_end = offset + extensions_length
    if extensions_end > len(hello):
        return None
    while offset + 4 <= extensions_end:
        extension_type = int.from_bytes(hello[offset:offset + 2], "big")
        extension_length = int.from_bytes(hello[offset + 2:offset + 4], "big")
        offset += 4
        extension = hello[offset:offset + extension_length]
        offset += extension_length
        if len(extension) != extension_length:
            return None
        if extension_type != 0 or len(extension) < 5:
            continue
        names_length = int.from_bytes(extension[0:2], "big")
        if names_length + 2 != len(extension):
            return None
        name_type = extension[2]
        name_length = int.from_bytes(extension[3:5], "big")
        if name_type != 0 or 5 + name_length > len(extension):
            return None
        try:
            return extension[5:5 + name_length].decode("ascii").lower().rstrip(".")
        except UnicodeDecodeError:
            return None
    return None


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    domains = [
        item.strip() for item in os.environ.get("MINI_PI_ALLOWED_DOMAINS", "").split(",")
        if item.strip()
    ]
    proxy = AllowlistProxy(domains)
    server = await asyncio.start_server(proxy.handle, "0.0.0.0", args.port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_main())
