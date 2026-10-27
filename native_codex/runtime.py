"""Existing kid runtime interface backed by protected native Windows Codex."""
import hmac
import http.server
import json
from pathlib import Path
import secrets
import sys
import threading

from codex_runtime import IsolationUnavailable
from .broker import BROKER_PORT
from .process import ProtectedProcess, metadata
from .wfp import ProtectionUnavailable, verify


class NativeRuntime:
    native = True

    def __init__(self, root, profile_home, endpoint, workspace=None, **kwargs):
        self.root, self.profile_home = Path(root), Path(profile_home)
        self.endpoint, self.workspace = endpoint, Path(workspace) if workspace else None
        self.protected = ProtectedProcess(root, profile_home, endpoint, workspace)
        self.stopped = self.protected.stopped
        self.process = self.reader = self.writer = None
        self.report = None
        self.on_tool = kwargs.get("on_tool")
        self.relay = None
        self.tool_port = None
        self.token = secrets.token_urlsafe(32)
        self.tool_slots = threading.BoundedSemaphore(16)

    @property
    def failure(self):
        return self.protected.failure

    def image_metadata(self):
        try:
            result = metadata(self.root, verify_hashes=False)
            verify(self.root / "codex_native" / "codex.exe", BROKER_PORT)
            return result
        except ProtectionUnavailable as error:
            raise IsolationUnavailable(str(error)) from None

    def start_worker(self):
        self.image_metadata()
        owner = self

        class Relay(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if (self.path != "/invoke" or self.headers.get("Origin")
                        or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + owner.token)):
                    self.send_error(403)
                    return
                if not owner.tool_slots.acquire(blocking=False):
                    self.send_error(503)
                    return
                try:
                    self.connection.settimeout(10)
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2 * 1024 * 1024 or owner.stopped.is_set():
                        raise ValueError()
                    request = json.loads(self.rfile.read(length))
                    result = owner.on_tool({"kind": "tool_call", "request": request})
                    body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:
                    self.send_error(502)
                finally:
                    owner.tool_slots.release()

        self.relay = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Relay)
        self.relay.daemon_threads = True
        self.tool_port = self.relay.server_port
        threading.Thread(target=self.relay.serve_forever, daemon=True).start()

    def start_server(self, search):
        try:
            self.process = self.protected.start(search)
        except ProtectionUnavailable as error:
            raise IsolationUnavailable(str(error)) from None
        self.reader, self.writer = self.process.stdout, self.process.stdin
        self.report = self.protected.report

    def thread_context(self, session_id, search):
        root = Path(__file__).resolve().parents[1]
        working = self.workspace or self.profile_home / "working"
        # kid confirms host actions in its own executor. Read-only prevents
        # built-in edits from bypassing that executor; never prevents a second
        # incompatible approval flow for the same local MCP action.
        return {"cwd": str(working), "approvalPolicy": "never", "sandbox": "read-only",
                "config": {"mcp_servers": {"kid": {"command": sys.executable,
                    "args": ["-I", str(root / "kid_mcp_server.py"), "--native", "--port", str(self.tool_port),
                             "--session", str(session_id), "--search", search],
                    "env": {"KID_MCP_TOKEN": self.token}, "enabled": True, "required": True,
                    "default_tools_approval_mode": "approve", "tool_timeout_sec": 300}}}}

    def close(self):
        self.protected.close()
        if self.relay:
            self.relay.shutdown()
            self.relay.server_close()
            self.relay = None
