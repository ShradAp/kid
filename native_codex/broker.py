"""Authenticated loopback CONNECT broker. Its only external dial is the proxy.

No requests/urllib/system proxy discovery, redirects or local destination DNS.
The listener closes on any relay failure. Credentials never enter the child.
"""
from __future__ import annotations

import base64
import hmac
import secrets
import select
import socket
import socketserver
import threading

from codex_proxy import ProxyUnavailable, connect_via_proxy
from codex_runtime import service_destination

BROKER_PORT = 47693


class Broker:
    def __init__(self, port=BROKER_PORT):
        self.lock = threading.RLock()
        self.clients = {}
        owner = self

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False
            request_queue_size = 32

            def server_bind(self):
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                upstream = None
                client = None
                try:
                    self.request.settimeout(8)
                    data = bytearray()
                    while not data.endswith(b"\r\n\r\n"):
                        if len(data) >= 16384:
                            return
                        part = self.request.recv(1)
                        if not part:
                            return
                        data.extend(part)
                    lines = data.decode("ascii").split("\r\n")
                    method, destination, version = lines[0].split()
                    if method != "CONNECT" or version not in ("HTTP/1.0", "HTTP/1.1"):
                        return
                    headers = {}
                    for line in lines[1:]:
                        if not line:
                            continue
                        key, value = line.split(":", 1)
                        if key.lower() in headers:
                            return
                        headers[key.lower()] = value.strip()
                    auth = headers.get("proxy-authorization", "")
                    with owner.lock:
                        client = next((item for key, item in owner.clients.items()
                                       if hmac.compare_digest(auth, key)), None)
                        if client is not None and not client["closed"] and len(client["sockets"]) < 64:
                            client["sockets"].add(self.request)
                        else:
                            client = None
                    if client is None:
                        self.request.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                        return
                    host, separator, port = destination.rpartition(":")
                    if not separator or not port.isdecimal() or not service_destination(host.lower(), int(port)):
                        self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                        return
                    # Real connection, not a preflight. Every socket goes through
                    # the existing numeric-IP, no-fallback proxy connector.
                    upstream = connect_via_proxy(client["endpoint"], host.lower(), int(port))
                    with owner.lock:
                        if client["closed"]:
                            return
                        client["sockets"].add(upstream)
                    self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    upstream.settimeout(10)
                    self.request.settimeout(10)
                    while not client["closed"]:
                        readable, _, _ = select.select([self.request, upstream], [], [], 1)
                        for source in readable:
                            payload = source.recv(32768)
                            if not payload:
                                return
                            (upstream if source is self.request else self.request).sendall(payload)
                except (OSError, ValueError, UnicodeError, ProxyUnavailable):
                    if client is not None and not client["closed"]:
                        owner.fail(client)
                finally:
                    with owner.lock:
                        if client:
                            client["sockets"].discard(self.request)
                            client["sockets"].discard(upstream)
                    if upstream:
                        upstream.close()

        self.server = Server(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def register(self, endpoint, on_failure):
        token = secrets.token_urlsafe(32)
        authorization = "Basic " + base64.b64encode(("kid:" + token).encode("ascii")).decode("ascii")
        with self.lock:
            self.clients[authorization] = {"endpoint": endpoint, "failure": on_failure,
                                           "sockets": set(), "closed": False}
        return authorization, f"http://kid:{token}@127.0.0.1:{self.port}"

    def fail(self, client):
        with self.lock:
            if client["closed"]:
                return
            client["closed"] = True
            sockets = list(client["sockets"])
        for connection in sockets:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        threading.Thread(target=client["failure"], daemon=True).start()

    def unregister(self, authorization):
        with self.lock:
            client = self.clients.pop(authorization, None)
        if client:
            # Normal close does not report a proxy failure.
            client["failure"] = lambda: None
            self.fail(client)

    def close(self):
        with self.lock:
            authorizations = list(self.clients)
        for authorization in authorizations:
            self.unregister(authorization)
        self.server.shutdown()
        self.server.server_close()


_shared = None
_shared_lock = threading.Lock()


def shared_broker():
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Broker()
        return _shared
