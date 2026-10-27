"""Real loopback proxy tests: destination DNS/direct sockets are forbidden."""
import json
import socket
import socketserver
import threading
import unittest
from unittest.mock import patch

from codex_proxy import ProxyEndpoint, ProxyUnavailable, connect_via_proxy, destination
from codex_runtime import service_destination


def read_exact(connection, count):
    result = bytearray()
    while len(result) < count:
        chunk = connection.recv(count - len(result))
        if not chunk:
            raise EOFError()
        result.extend(chunk)
    return bytes(result)


class FixtureProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


class ProxyTests(unittest.TestCase):
    def start(self, handler):
        server = FixtureProxy(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join(timeout=2)))
        return server

    def test_http_authentication_refusal_never_connects_destination(self):
        proxy_calls, direct_calls = [], []
        class Target(socketserver.BaseRequestHandler):
            def handle(self):
                direct_calls.append(True)
        target = self.start(Target)
        class RefusingProxy(socketserver.BaseRequestHandler):
            def handle(self):
                raw = bytearray()
                while not raw.endswith(b"\r\n\r\n"):
                    raw.extend(read_exact(self.request, 1))
                proxy_calls.append(bytes(raw))
                self.request.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length:0\r\n\r\n")
        proxy = self.start(RefusingProxy)
        endpoint = ProxyEndpoint.parse(f"http://user@127.0.0.1:{proxy.server_address[1]}", "private-password")
        with self.assertRaises(ProxyUnavailable) as error:
            connect_via_proxy(endpoint, "127.0.0.1", target.server_address[1], timeout=2)
        self.assertEqual(len(proxy_calls), 1)
        self.assertEqual(direct_calls, [])
        self.assertNotIn("private-password", str(error.exception))
        self.assertNotIn("private-password", repr(endpoint))
        self.assertIn(b"Proxy-Authorization: Basic ", proxy_calls[0])

    def test_http_tunnel_does_not_resolve_destination_locally(self):
        requests = []
        class Proxy(socketserver.BaseRequestHandler):
            def handle(self):
                raw = bytearray()
                while not raw.endswith(b"\r\n\r\n"):
                    raw.extend(read_exact(self.request, 1))
                requests.append(bytes(raw))
                self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                self.request.sendall(read_exact(self.request, 4))
        proxy = self.start(Proxy)
        endpoint = ProxyEndpoint.parse(f"http://127.0.0.1:{proxy.server_address[1]}")
        with patch("socket.getaddrinfo", side_effect=AssertionError("No host DNS")):
            with connect_via_proxy(endpoint, "does-not-resolve.invalid", 443, timeout=2) as tunnel:
                tunnel.sendall(b"ping")
                self.assertEqual(read_exact(tunnel, 4), b"ping")
        self.assertIn(b"CONNECT does-not-resolve.invalid:443 HTTP/1.1", requests[0])

    def test_socks_uses_authentication_and_remote_domain_resolution(self):
        seen = []
        class SocksProxy(socketserver.BaseRequestHandler):
            def handle(self):
                self.assertions = read_exact(self.request, 3)
                self.request.sendall(b"\x05\x02")
                version, user_size = read_exact(self.request, 2)
                user = read_exact(self.request, user_size)
                password_size = read_exact(self.request, 1)[0]
                password = read_exact(self.request, password_size)
                seen.append((version, user, password))
                self.request.sendall(b"\x01\x00")
                request = read_exact(self.request, 5)
                name = read_exact(self.request, request[4])
                port = int.from_bytes(read_exact(self.request, 2), "big")
                seen.append((request[:4], name, port))
                self.request.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
                self.request.sendall(read_exact(self.request, 4))
        proxy = self.start(SocksProxy)
        endpoint = ProxyEndpoint.parse(f"socks5h://user@127.0.0.1:{proxy.server_address[1]}", "secret")
        with patch("socket.getaddrinfo", side_effect=AssertionError("No host DNS")):
            with connect_via_proxy(endpoint, "example.invalid", 443, timeout=2) as tunnel:
                tunnel.sendall(b"ping")
                self.assertEqual(read_exact(tunnel, 4), b"ping")
        self.assertEqual(seen[0], (1, b"user", b"secret"))
        self.assertEqual(seen[1], (b"\x05\x01\x00\x03", b"example.invalid", 443))

    def test_proxy_disconnect_has_no_second_route(self):
        calls = []
        class DroppingProxy(socketserver.BaseRequestHandler):
            def handle(self):
                calls.append(True)
        proxy = self.start(DroppingProxy)
        with self.assertRaises(ProxyUnavailable):
            connect_via_proxy(ProxyEndpoint.parse(f"http://127.0.0.1:{proxy.server_address[1]}"), "chatgpt.com", 443, timeout=2)
        self.assertEqual(calls, [True])

    def test_proxy_must_have_numeric_address_and_separate_password(self):
        for url in ("http://proxy.example.com:8080", "http://u:secret@127.0.0.1:8080", "file://127.0.0.1:80", "http://127.0.0.1:80/path"):
            with self.subTest(url=url), self.assertRaises(ProxyUnavailable):
                ProxyEndpoint.parse(url)
        self.assertEqual(ProxyEndpoint.parse("http://localhost:8080").host, "127.0.0.1")

    def test_destination_injection_is_rejected_before_dial(self):
        for host, port in (("chatgpt.com\r\nInjected: yes", 443), ("user@chatgpt.com", 443), ("chatgpt.com", True), ("chatgpt.com", 0)):
            with self.subTest(host=host), patch("socket.socket") as factory:
                with self.assertRaises(ProxyUnavailable):
                    connect_via_proxy(ProxyEndpoint.parse("http://127.0.0.1:80"), host, port)
                factory.assert_not_called()

    def test_runtime_allowlist_cannot_turn_into_generic_browser(self):
        for host in ("chatgpt.com", "auth.openai.com", "ab.chatgpt.com"):
            self.assertTrue(service_destination(host, 443))
        for host in ("chatgpt.com.evil.test", "evilopenai.com", "google.com", "127.0.0.1"):
            self.assertFalse(service_destination(host, 443))
        self.assertFalse(service_destination("chatgpt.com", 80))


if __name__ == "__main__":
    unittest.main()
