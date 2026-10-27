"""The only host-side Internet socket allowed for the isolated Codex worker.

Every TCP socket connects to one numeric upstream proxy address. Destination
names are passed to CONNECT/SOCKS, never resolved by the host. No environment,
system proxy, DNS, redirect, retry or direct-connect fallback is consulted.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import ipaddress
import http.client
import json
import re
import socket
import ssl
from urllib.parse import unquote, urlsplit


class ProxyUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Прокси недоступен. Запрос Codex заблокирован")


@dataclass(frozen=True)
class ProxyEndpoint:
    host: str
    port: int
    scheme: str
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)

    @classmethod
    def parse(cls, url, password=""):
        try:
            parsed = urlsplit(url)
            host = "127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname
            host = str(ipaddress.ip_address(host))
            if (parsed.scheme not in ("http", "https", "socks5h") or not parsed.port
                    or parsed.password is not None or parsed.path not in ("", "/")
                    or parsed.query or parsed.fragment):
                raise ValueError()
            username = unquote(parsed.username or "")
            if any(char in username + password for char in "\r\n\0"):
                raise ValueError()
            return cls(host, parsed.port, parsed.scheme, username, password)
        except (ValueError, TypeError):
            raise ProxyUnavailable() from None


def destination(host, port):
    """Reject header injection and ambiguous destination syntax."""
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ProxyUnavailable()
    if not isinstance(host, str) or not host or len(host) > 253:
        raise ProxyUnavailable()
    try:
        address = ipaddress.ip_address(host)
        name = str(address)
        authority = f"[{name}]:{port}" if address.version == 6 else f"{name}:{port}"
    except ValueError:
        try:
            name = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ProxyUnavailable() from None
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", name) or ".." in name:
            raise ProxyUnavailable()
        authority = f"{name}:{port}"
    return name, authority


def _read_exact(connection, count):
    parts = bytearray()
    while len(parts) < count:
        chunk = connection.recv(count - len(parts))
        if not chunk:
            raise ProxyUnavailable()
        parts.extend(chunk)
    return bytes(parts)


def _http_header(connection):
    header = bytearray()
    while not header.endswith(b"\r\n\r\n"):
        if len(header) >= 16 * 1024:
            raise ProxyUnavailable()
        header.extend(_read_exact(connection, 1))
    return bytes(header)


def connect_via_proxy(endpoint: ProxyEndpoint, host: str, port: int, timeout=15):
    """Return an established tunnel, or fail without any alternative dial."""
    connection = None
    try:
        name, authority = destination(host, port)
        # No getaddrinfo: address family and numeric destination are explicit.
        numeric = ipaddress.ip_address(endpoint.host)
        connection = socket.socket(socket.AF_INET6 if numeric.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
        connection.settimeout(timeout)
        connection.connect((str(numeric), endpoint.port))
        if endpoint.scheme == "https":
            connection = ssl.create_default_context().wrap_socket(connection, server_hostname=endpoint.host)
        if endpoint.scheme in ("http", "https"):
            header = f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
            if endpoint.username or endpoint.password:
                credentials = base64.b64encode((endpoint.username + ":" + endpoint.password).encode("utf-8")).decode("ascii")
                header += "Proxy-Authorization: Basic " + credentials + "\r\n"
            connection.sendall((header + "\r\n").encode("ascii"))
            response = _http_header(connection).split(b"\r\n", 1)[0].split()
            if len(response) < 2 or response[1] != b"200":
                raise ProxyUnavailable()
        elif endpoint.scheme == "socks5h":
            authenticated = bool(endpoint.username or endpoint.password)
            connection.sendall(b"\x05\x01\x02" if authenticated else b"\x05\x01\x00")
            if _read_exact(connection, 2) != (b"\x05\x02" if authenticated else b"\x05\x00"):
                raise ProxyUnavailable()
            if authenticated:
                username, password = endpoint.username.encode("utf-8"), endpoint.password.encode("utf-8")
                if len(username) > 255 or len(password) > 255:
                    raise ProxyUnavailable()
                connection.sendall(b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password)
                if _read_exact(connection, 2) != b"\x01\x00":
                    raise ProxyUnavailable()
            # Domain form delegates ALL destination DNS to the proxy, even for
            # a destination whose textual spelling looks like an IP address.
            encoded = name.encode("ascii")
            connection.sendall(b"\x05\x01\x00\x03" + bytes([len(encoded)]) + encoded + port.to_bytes(2, "big"))
            version, result, reserved, address_type = _read_exact(connection, 4)
            if (version, result, reserved) != (5, 0, 0):
                raise ProxyUnavailable()
            size = {1: 4, 4: 16}.get(address_type)
            if address_type == 3:
                size = _read_exact(connection, 1)[0]
            if size is None:
                raise ProxyUnavailable()
            _read_exact(connection, size + 2)
        else:
            raise ProxyUnavailable()
        return connection
    except Exception:
        if connection is not None:
            connection.close()
        # Upstream errors/headers can contain credentials. Discard them.
        raise ProxyUnavailable() from None


def probe_exit(endpoint, timeout=15):
    """One TLS-verified IP check THROUGH the proxy; never learns a direct IP."""
    with connect_via_proxy(endpoint, "api.ipify.org", 443, timeout) as tunnel:
        with ssl.create_default_context().wrap_socket(tunnel, server_hostname="api.ipify.org") as secure:
            secure.sendall(b"GET /?format=json HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\nAccept: application/json\r\n\r\n")
            header = _http_header(secure)
            if header.split(b"\r\n", 1)[0].split()[1:2] != [b"200"]:
                raise ProxyUnavailable()
            chunks = bytearray()
            while len(chunks) < 16384:
                chunk = secure.recv(4096)
                if not chunk:
                    break
                chunks.extend(chunk)
            try:
                # ipify supplies Content-Length. Fail safely on unsupported
                # chunked/oversized/malformed responses rather than guessing.
                ip = str(ipaddress.ip_address(json.loads(chunks)["ip"]))
            except (ValueError, KeyError, TypeError):
                raise ProxyUnavailable() from None
            return {"exit_ip": ip, "transport": endpoint.scheme, "tls_verified": True}


def https_get(endpoint, host, path, max_bytes=64 * 1024 * 1024, timeout=30):
    """TLS-verified download for provisioning. Redirects are rejected."""
    class ProxiedHTTPS(http.client.HTTPSConnection):
        def connect(self):
            tunnel = connect_via_proxy(endpoint, self.host, self.port, timeout)
            try:
                self.sock = ssl.create_default_context().wrap_socket(tunnel, server_hostname=self.host)
            except Exception:
                tunnel.close()
                raise ProxyUnavailable() from None
    connection = ProxiedHTTPS(host, timeout=timeout)
    try:
        connection.request("GET", path, headers={"User-Agent": "kid-codex-setup/1", "Accept": "application/json, application/octet-stream"})
        response = connection.getresponse()
        if response.status != 200:
            raise ProxyUnavailable()
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ProxyUnavailable()
        return body
    except Exception:
        raise ProxyUnavailable() from None
    finally:
        connection.close()
