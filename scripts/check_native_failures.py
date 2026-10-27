"""Real native client vs controlled broken proxies; no direct service baseline."""
import json
from pathlib import Path
import socketserver
import sys
import threading
import time
import socket

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_paths import USER_DATA_DIR
from codex_engine import StdioConnection, profile_home
from codex_proxy import ProxyEndpoint
from native_codex.process import ProtectedProcess
from native_codex.wfp import verify


def check(mode):
    observed = []
    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(4)
            data = bytearray()
            while not data.endswith(b"\r\n\r\n") and len(data) < 16384:
                part = self.request.recv(1)
                if not part:
                    return
                data.extend(part)
            observed.append(True)  # Never retain headers or credentials.
            if mode == "auth_407":
                self.request.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
            else:
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.request.recv(8192)
                # Actual TLS stream is interrupted; no TLS termination/auth read.
    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    if mode == "unavailable":
        server.shutdown()
        server.server_close()
    runtime = ProtectedProcess(USER_DATA_DIR, profile_home(USER_DATA_DIR, "codex"),
                               ProxyEndpoint.parse(f"http://127.0.0.1:{port}"))
    connection = None
    try:
        process = runtime.start("codex")
        connection = StdioConnection(process.stdout, process.stdin)
        request_failed = False
        try:
            connection.initialize()
            connection.request("account/rateLimits/read", timeout=20)
        except Exception:
            request_failed = True
        assert request_failed, "Broken proxy must fail the real client request"
        if mode == "drop_tls":
            # EOF is not by itself a global proxy error: the application must
            # still fail its actual TLS request, with no direct route available.
            assert observed
        else:
            assert runtime.stopped.wait(5), "Failed CONNECT must stop native Codex"
            if mode == "auth_407":
                assert observed, "Failure must actually come from the rejecting proxy"
        print(json.dumps({"scenario": mode, "blocked": True, "proxy_connections": len(observed),
                          "wfp_proof": runtime.report["proof"], "generation_requests": 0}))
    finally:
        if connection:
            connection.close()
        runtime.close()
        if mode != "unavailable":
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    for executable in ("codex.exe", "network-probe.exe", "codex-code-mode-host.exe"):
        verify(USER_DATA_DIR / "codex_native" / executable, 47693)
    try:
        # Ordinary host networking is allowed. This is a TCP handshake to a
        # non-OpenAI address, never a direct OpenAI/Codex baseline request.
        with socket.create_connection(("1.1.1.1", 443), timeout=4):
            internet = True
    except OSError:
        internet = False
    print(json.dumps({"ordinary_host_internet_available": internet, "direct_OpenAI_requests": 0}))
    for mode in ("unavailable", "auth_407", "drop_tls"):
        check(mode)
