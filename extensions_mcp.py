"""Small MCP stdio client and lazy skill reader. No package installation."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading


class MCPClient:
    def __init__(self, connection, timeout=15, secret_values=None):
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"}}
        values = secret_values if secret_values is not None else os.environ
        for key, reference in connection.get("env_from", {}).items():
            if reference not in values:
                raise ValueError("Не задана переменная окружения: " + reference)
            environment[key] = values[reference]
        self.timeout, self.sequence = timeout, 0
        self.messages = queue.Queue(maxsize=256)
        self.job = None
        self.process = subprocess.Popen([connection["command"], *connection.get("args", [])],
            cwd=connection.get("cwd") or None, env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            if os.name == "nt":
                from native_codex.job import ProcessJob
                self.job = ProcessJob()
                self.job.assign(self.process)
            threading.Thread(target=self._read, daemon=True).start()
            initialized = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "kid", "version": "1"}})
            self.info = initialized.get("serverInfo", {})
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(2 * 1024 * 1024 + 1)
                if not line or len(line) > 2 * 1024 * 1024:
                    break
                self.messages.put_nowait(json.loads(line))
        except (ValueError, OSError, queue.Full):
            pass
        finally:
            try:
                self.messages.put_nowait(None)
            except queue.Full:
                pass

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method, params=None):
        import time
        self.sequence += 1
        identity = self.sequence
        self.send({"jsonrpc": "2.0", "id": identity, "method": method, "params": params or {}})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                message = self.messages.get(timeout=max(.001, deadline - time.monotonic()))
            except queue.Empty:
                raise ValueError("MCP не ответил вовремя") from None
            if message is None:
                raise ValueError("MCP закрыл соединение или вернул некорректный JSON")
            if message.get("method") and "id" in message:
                self.send({"jsonrpc": "2.0", "id": message["id"], "error": {
                    "code": -32601, "message": "Client capability unavailable"}})
            elif message.get("id") == identity:
                if "error" in message or not isinstance(message.get("result"), dict):
                    raise ValueError("MCP отклонил запрос " + method)
                return message["result"]
            if time.monotonic() >= deadline:
                raise ValueError("MCP не ответил вовремя")

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        if self.job:
            self.job.close()
        self.process.wait(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()


SKILL_TOOL = {"name": "read_skill", "description": "Read the full instructions of an enabled kid skill by its catalog ID, only when needed.",
    "inputSchema": {"type": "object", "properties": {"id": {"type": "string", "description": "Skill catalog ID, or library extension ID if it contains exactly one skill"},
        "resource": {"type": "string", "description": "Optional relative text resource path inside the skill, e.g. references/guide.md"}},
        "required": ["id"], "additionalProperties": False}}


def serve(snapshot_path):
    snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    skills = {item["id"]: item for item in snapshot.get("skills", [])}
    # Installation returns the library ID. Accept it for a single-skill entry,
    # while keeping component IDs unambiguous for packages with multiple skills.
    groups = {}
    for item in snapshot.get("skills", []):
        groups.setdefault(item["id"].split("_", 1)[0], []).append(item)
    skills.update({identity: items[0] for identity, items in groups.items() if len(items) == 1})
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            if "id" not in request:
                continue
            method, params = request.get("method"), request.get("params") or {}
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}}, "serverInfo": {"name": "kid-skills", "version": "1"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [SKILL_TOOL] if skills else []}
            elif method == "tools/call" and params.get("name") == "read_skill":
                skill = skills[(params.get("arguments") or {})["id"]]
                folder = Path(skill["path"]).parent.resolve()
                resource = (params.get("arguments") or {}).get("resource", "SKILL.md")
                target = (folder / resource).resolve()
                if not target.is_relative_to(folder) or target.stat().st_size > 256 * 1024:
                    raise ValueError()
                result = {"content": [{"type": "text", "text": "Instruction/resource file: " + str(target) + "\n\n" + target.read_text(encoding="utf-8")}]}
            else:
                raise ValueError()
            answer = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        except Exception:
            answer = {"jsonrpc": "2.0", "id": request.get("id"),
                "error": {"code": -32602, "message": "Skill unavailable in this session"}}
        sys.stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    serve(sys.argv[1])
