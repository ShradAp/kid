"""Trusted stdio multiplexer inside the network-less Linux container.

This file is baked into a read-only image, never imported from the model's
workspace. No upstream address/password exists in the container. Its loopback
CONNECT listener can only ask the host broker to establish a proxied tunnel.
"""
import base64
import errno
import json
import http.server
import os
from pathlib import Path
import queue
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit

MAX_FRAME = 8 * 1024 * 1024
WRITE_LOCK = threading.Lock()
TUNNELS = {}
TUNNEL_LOCK = threading.Lock()
PROCESS = None
TOOLS = {}
STOP = threading.Event()
LAST_HEARTBEAT = time.monotonic()


def emit(kind, **fields):
    with WRITE_LOCK:
        sys.stdout.write(json.dumps({"kind": kind, **fields}, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def verify_kernel():
    interfaces = sorted(p.name for p in Path("/sys/class/net").iterdir())
    if interfaces != ["lo"] or os.getuid() == 0:
        raise RuntimeError("Isolation unavailable")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    if int(status["CapEff"].strip(), 16) or int(status["CapBnd"].strip(), 16) or status["NoNewPrivs"].strip() != "1":
        raise RuntimeError("Isolation unavailable")
    if not os.statvfs("/").f_flag & os.ST_RDONLY:
        raise RuntimeError("Writable root")
    for line in Path("/proc/net/route").read_text().splitlines()[1:]:
        if line.split()[0] != "lo":
            raise RuntimeError("External route")
    for line in Path("/proc/net/ipv6_route").read_text().splitlines():
        if line.split()[-1] != "lo":
            raise RuntimeError("External IPv6 route")
    # Only AFTER verifying the namespace: real IPv4/IPv6 TCP/UDP attempts must
    # fail in the kernel. A separate child proves inheritance of that boundary.
    check = '''
import socket
checks=0
for family,address in [(socket.AF_INET,('1.1.1.1',443)),(socket.AF_INET6,('2606:4700:4700::1111',443))]:
 for kind in [socket.SOCK_STREAM,socket.SOCK_DGRAM]:
  with socket.socket(family,kind) as s:
   s.settimeout(.5)
   try:
    if kind==socket.SOCK_STREAM: s.connect(address)
    else: s.sendto(b'isolated',address)
   except OSError: checks+=1
   else: raise SystemExit(2)
print(checks)
'''
    result = subprocess.run([sys.executable, "-I", "-c", check], capture_output=True, timeout=8)
    if result.returncode or result.stdout.strip() != b"4":
        raise RuntimeError("Direct traffic was not blocked")
    return {"interfaces": interfaces, "child_direct_checks_blocked": 4,
            "capabilities": 0, "no_new_privileges": True, "root_read_only": True}


class TunnelHandler(socketserver.BaseRequestHandler):
    def handle(self):
        identity = uuid.uuid4().hex
        self.request.settimeout(25)
        replies = queue.Queue(maxsize=32)
        with TUNNEL_LOCK:
            if len(TUNNELS) >= 32:
                return
            TUNNELS[identity] = replies
        try:
            header = bytearray()
            while not header.endswith(b"\r\n\r\n"):
                if len(header) > 16384:
                    return
                byte = self.request.recv(1)
                if not byte:
                    return
                header.extend(byte)
            line = header.split(b"\r\n", 1)[0].decode("ascii").split()
            if len(line) != 3 or not line[2].startswith("HTTP/1."):
                self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            connect_mode = line[0] == "CONNECT"
            if connect_mode:
                host, separator, port = line[1].rpartition(":")
                if not separator or not port.isdigit():
                    return
                host, port = host.strip("[]"), int(port)
                first_data = None
            elif line[0] in ("GET", "HEAD"):
                url = urlsplit(line[1])
                if url.scheme != "http" or not url.hostname or url.username:
                    return
                host, port = url.hostname, url.port or 80
                path = (url.path or "/") + ("?" + url.query if url.query else "")
                lines = header.decode("ascii").split("\r\n")
                lines[0] = f"{line[0]} {path} {line[2]}"
                lines = [item for item in lines if not item.lower().startswith(("proxy-authorization:", "proxy-connection:"))]
                first_data = "\r\n".join(lines).encode("ascii")
            else:
                return
            emit("dial", id=identity, host=host, port=port)
            answer = replies.get(timeout=25)
            if answer.get("kind") != "dial_result" or not answer.get("ok"):
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return
            if connect_mode:
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                emit("data", id=identity, data=base64.b64encode(first_data).decode("ascii"))
            self.request.settimeout(1)
            def receive():
                try:
                    while not STOP.is_set():
                        frame = replies.get(timeout=120)
                        if frame.get("kind") != "data":
                            break
                        self.request.sendall(base64.b64decode(frame["data"], validate=True))
                except (OSError, ValueError, KeyError, queue.Empty):
                    pass
                finally:
                    try:
                        self.request.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            receiver = threading.Thread(target=receive, daemon=True)
            receiver.start()
            while not STOP.is_set():
                try:
                    chunk = self.request.recv(32768)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                emit("data", id=identity, data=base64.b64encode(chunk).decode("ascii"))
        except (OSError, ValueError, UnicodeError, KeyError, queue.Empty):
            pass
        finally:
            with TUNNEL_LOCK:
                TUNNELS.pop(identity, None)
            emit("close", id=identity)


class LoopbackProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


class ToolRelay(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path not in ("/invoke", "/model") or self.headers.get("Origin"):
            self.send_error(403)
            return
        identity = uuid.uuid4().hex
        replies = queue.Queue(maxsize=1)
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 2 * 1024 * 1024:
                raise ValueError()
            request = json.loads(self.rfile.read(length))
            with TUNNEL_LOCK:
                if len(TOOLS) >= 16:
                    raise ValueError()
                TOOLS[identity] = replies
            emit("tool_call" if self.path == "/invoke" else "model_call", id=identity, request=request)
            result = replies.get(timeout=180)
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            try:
                self.send_error(502)
            except OSError:
                pass
        finally:
            with TUNNEL_LOCK:
                TOOLS.pop(identity, None)


def worker_environment(proxy_port):
    proxy = f"http://127.0.0.1:{proxy_port}"
    return {"PATH": "/opt/kid/bin:/opt/kid/codex-path:/usr/local/bin:/usr/bin:/bin", "HOME": "/home/codex",
            "CODEX_HOME": "/profile", "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
            "ALL_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy, "all_proxy": proxy,
            "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1",
            "BROWSER": "/opt/kid/deny-browser", "LANG": "C.UTF-8", "TERM": "dumb",
            "KID_CONFIG_DIR": "/tools_data", "KID_STRICT_PROXY": "1"}


def execute_local(frame, proxy_port, tool_port):
    try:
        request = {**frame.get("request", {}), "relay_port": tool_port, "proxy_port": proxy_port}
        process = subprocess.Popen([sys.executable, "-I", "/opt/kid/kid_tool_worker.py"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, encoding="utf-8", env=worker_environment(proxy_port))
        output, _ = process.communicate(json.dumps(request), timeout=170)
        result = json.loads(output) if process.returncode == 0 and len(output) <= 200000 else {"error": "Инструмент недоступен"}
        emit("tool_executed", result=result)
    except Exception:
        emit("tool_executed", result={"error": "Выполнение инструмента остановлено"})


def spawn_server(proxy_port, search):
    global PROCESS
    if PROCESS is not None:
        raise RuntimeError("Already started")
    environment = worker_environment(proxy_port)
    command = ["/opt/kid/bin/codex", "app-server", "--listen", "stdio://",
               "-c", 'cli_auth_credentials_store="file"', "-c", 'forced_login_method="chatgpt"',
               "-c", 'analytics.enabled=false', "-c", 'feedback.enabled=false',
               "-c", 'check_for_update_on_startup=false',
               "-c", 'web_search="live"' if search == "codex" else 'web_search="disabled"']
    for feature in ("system_proxy_fallback", "unbounded_connection_retries", "apps", "browser_use",
                    "browser_use_external", "browser_use_full_cdp_access", "computer_use", "remote_plugin",
                    "skill_mcp_dependency_install", "daemon_auto_start"):
        command.extend(["--disable", feature])
    # A generic shell could synthesize its own web search or launch another
    # model client. The kid-search route cannot expose it.
    # Host operations are provided by closed Windows MCP tools. A Linux shell
    # must not fabricate a Desktop or execute operations in the wrong machine.
    for feature in ("shell_tool", "unified_exec", "code_mode", "code_mode_host"):
        command.extend(["--disable", feature])
    PROCESS = subprocess.Popen(command, cwd="/home/codex", env=environment,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding="utf-8", bufsize=1, start_new_session=True)
    def read_server():
        try:
            for line in PROCESS.stdout:
                if len(line) > MAX_FRAME:
                    break
                message = json.loads(line)
                if isinstance(message, dict):
                    emit("server", message=message)
        except (ValueError, OSError):
            pass
        finally:
            emit("server_closed")
    threading.Thread(target=read_server, daemon=True).start()
    emit("started")


def main():
    global LAST_HEARTBEAT
    report = verify_kernel()
    # Prevent same-uid tool processes from reading the supervisor's memory.
    import ctypes
    if ctypes.CDLL(None).prctl(4, 0) != 0:  # PR_SET_DUMPABLE
        raise RuntimeError("Supervisor isolation unavailable")
    with LoopbackProxy(("127.0.0.1", 0), TunnelHandler) as proxy, http.server.ThreadingHTTPServer(("127.0.0.1", 0), ToolRelay) as tool_relay:
        LAST_HEARTBEAT = time.monotonic()
        def watchdog():
            while not STOP.wait(2):
                if time.monotonic() - LAST_HEARTBEAT > 20:
                    os._exit(1)  # PID 1 exit kills the whole container process tree
        threading.Thread(target=watchdog, daemon=True).start()
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        threading.Thread(target=tool_relay.serve_forever, daemon=True).start()
        emit("ready", report=report, proxy_port=proxy.server_address[1], tool_port=tool_relay.server_port)
        try:
            while True:
                line = sys.stdin.readline(MAX_FRAME + 1)
                if not line or len(line) > MAX_FRAME:
                    break
                frame = json.loads(line)
                kind = frame.get("kind")
                if kind == "heartbeat":
                    LAST_HEARTBEAT = time.monotonic()
                elif kind == "start":
                    spawn_server(proxy.server_address[1], frame.get("search"))
                elif kind == "rpc" and PROCESS is not None:
                    PROCESS.stdin.write(json.dumps(frame["message"]) + "\n")
                    PROCESS.stdin.flush()
                elif kind in ("data", "close", "dial_result"):
                    with TUNNEL_LOCK:
                        target = TUNNELS.get(frame.get("id"))
                    if target is not None:
                        try:
                            target.put_nowait(frame)
                        except queue.Full:
                            break
                elif kind == "tool_result":
                    with TUNNEL_LOCK:
                        target = TOOLS.get(frame.get("id"))
                    if target is not None:
                        target.put_nowait(frame.get("result", {}))
                elif kind == "execute_tool" and PROCESS is None:
                    threading.Thread(target=execute_local, args=(frame, proxy.server_address[1], tool_relay.server_port), daemon=True).start()
                elif kind == "stop":
                    break
                else:
                    raise RuntimeError("Unknown worker frame")
        finally:
            STOP.set()
            if PROCESS is not None:
                try:
                    os.killpg(PROCESS.pid, signal.SIGKILL)
                except OSError:
                    pass
                PROCESS.wait(timeout=5)
            proxy.shutdown()
            tool_relay.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never include an input frame, child output or credentials in logs.
        emit("fatal", code="isolation_or_worker_failure")
        raise SystemExit(1)
