"""Exercise proxy failures only AFTER proving real kernel egress containment."""
import json
from pathlib import Path
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_paths import USER_DATA_DIR
from codex_proxy import ProxyEndpoint
from codex_runtime import IsolatedRuntime, IsolationUnavailable


class Proxy(socketserver.ThreadingTCPServer):
    daemon_threads = True


def exercise(mode):
    calls = []
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            calls.append(True)
            self.request.settimeout(3)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = self.request.recv(1)
                if not chunk:
                    return
                data += chunk
            if mode == "auth_407":
                self.request.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
            else:
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.request.recv(4096)  # fail during actual TLS data, not a preflight
    with Proxy(("127.0.0.1", 0), Handler) as proxy, tempfile.TemporaryDirectory(prefix="kid-proxy-failure-") as directory:
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        port = proxy.server_address[1]
        if mode == "unavailable":
            proxy.shutdown(); proxy.server_close(); thread.join(timeout=3)
        root = Path(directory)
        (root / "codex_runtime").mkdir()
        shutil.copyfile(USER_DATA_DIR / "codex_runtime" / "runtime.json", root / "codex_runtime" / "runtime.json")
        endpoint = ProxyEndpoint.parse(f"http://127.0.0.1:{port}")
        runtime = IsolatedRuntime(root, root / "worker", endpoint)
        attempts = []
        original = socket.socket.connect
        def pinned(sock, address):
            attempts.append(address)
            if address != ("127.0.0.1", port):
                raise AssertionError("Direct host destination attempted")
            return original(sock, address)
        try:
            # No model/account process is launched until this proof succeeds.
            runtime.start_worker()
            with patch.object(socket.socket, "connect", pinned):
                code = ("import urllib.request; p='http://127.0.0.1:" + str(runtime.proxy_port) + "'; "
                        "o=urllib.request.build_opener(urllib.request.ProxyHandler({'https':p})); "
                        "o.open('https://api.ipify.org',timeout=8)")
                result = subprocess.run(runtime.docker_command("exec", runtime.container, "/usr/local/bin/python", "-I", "-c", code),
                               capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                assert result.returncode != 0, "Broken proxy must fail the actual TLS request"
                if mode != "drop_during_tls":
                    assert runtime.stopped.wait(10), "Proxy dial failure must close the worker"
                assert len(attempts) == 1 and attempts[0] == ("127.0.0.1", port)
            print(json.dumps({"scenario": mode, "proxy_attempts": len(attempts), "direct_host_attempts": 0,
                              "worker_direct_transports_blocked": runtime.report["child_direct_checks_blocked"], "request_blocked": True}))
        finally:
            runtime.close()
            if mode != "unavailable":
                proxy.shutdown(); thread.join(timeout=3)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    for mode in ("unavailable", "auth_407", "drop_during_tls"):
        exercise(mode)
    # Lost host broker: the trusted container PID 1 must terminate all children.
    with tempfile.TemporaryDirectory(prefix="kid-watchdog-") as directory:
        runtime = IsolatedRuntime(USER_DATA_DIR, Path(directory), ProxyEndpoint.parse("http://127.0.0.1:1"))
        normal_send = runtime.send
        runtime.send = lambda frame: None if frame.get("kind") == "heartbeat" else normal_send(frame)
        try:
            runtime.start_worker()
            assert runtime.stopped.wait(30), "Missing broker heartbeat must stop worker"
            print(json.dumps({"broker_loss_watchdog_stops_worker": True}))
        finally:
            runtime.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, (IsolationUnavailable, AssertionError)) else type(error).__name__)
        raise SystemExit(1)
