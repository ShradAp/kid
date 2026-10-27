"""Docker network-none launcher and the host side of the stdio proxy broker."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import subprocess
import threading

from codex_proxy import ProxyEndpoint, ProxyUnavailable, connect_via_proxy

MAX_FRAME = 8 * 1024 * 1024
SERVICE_DOMAINS = ("openai.com", "chatgpt.com")


def runtime_for(root, profile_home, endpoint, workspace=None):
    # New Windows installations always use the protected native transport.
    # Legacy Docker remains available only to its explicit diagnostic scripts.
    if os.name == "nt":
        from native_codex.runtime import NativeRuntime
        return NativeRuntime(root, profile_home, endpoint, workspace)
    raise IsolationUnavailable("Этот клиент Codex требует Windows")


def runtime_sources():
    root = Path(__file__).resolve().parent
    direct = {name: (root / name).read_bytes() for name in ("codex_worker.py", "kid_mcp_server.py", "kid_tool_worker.py")}
    excluded = {"desktop_app.py", "win_tools.py", "code_workspace.py", "bridge_security.py", "process_cleanup.py"}
    for path in root.glob("*.py"):
        if path.name not in excluded and path.name not in direct:
            direct["kidlib/" + path.name] = path.read_bytes()
    for directory in ("memory", "services", "network_agent"):
        for path in (root / directory).rglob("*.py"):
            direct["kidlib/" + path.relative_to(root).as_posix()] = path.read_bytes()
    direct["kidlib/config.default.json"] = (root / "config.default.json").read_bytes()
    return direct


def source_fingerprint():
    digest = hashlib.sha256()
    for name, content in sorted(runtime_sources().items()):
        digest.update(name.encode())
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


class IsolationUnavailable(RuntimeError):
    pass


def service_destination(host, port):
    return (port == 443 and isinstance(host, str) and
            (host == "api.ipify.org" or any(host == domain or host.endswith("." + domain) for domain in SERVICE_DOMAINS)))


class MessageReader:
    def __init__(self):
        self.messages = queue.Queue(maxsize=512)

    def readline(self, limit):
        return self.messages.get()

    def put(self, message):
        try:
            self.messages.put_nowait(json.dumps(message) + "\n" if message else "")
        except queue.Full:
            # A slow consumer must not turn child output into unbounded RAM.
            raise IsolationUnavailable("Переполнен канал App Server") from None


class MessageWriter:
    def __init__(self, runtime):
        self.runtime = runtime

    def write(self, line):
        self.runtime.send({"kind": "rpc", "message": json.loads(line)})

    def flush(self):
        pass


class IsolatedRuntime:
    def __init__(self, root, profile_home, endpoint: ProxyEndpoint, workspace=None, network_scope="codex", on_tool=None):
        self.root = Path(root)
        self.profile_home = Path(profile_home)
        self.endpoint = endpoint
        self.workspace = Path(workspace).resolve() if workspace else None
        self.reader, self.writer = MessageReader(), MessageWriter(self)
        self.write_lock = threading.Lock()
        self.lock = threading.RLock()
        self.events = queue.Queue(maxsize=32)
        self.tunnels = {}
        self.pending_dials = set()
        self.stopped = threading.Event()
        self.failure = None
        self.process = None
        self.container = None
        self.report = None
        self.proxy_port = None
        self.tool_port = None
        self.network_scope = network_scope
        self.on_tool = on_tool

    def docker_command(self, *arguments):
        # No remote DOCKER_HOST/context, credential helpers or registry login.
        executable = shutil.which("docker")
        if os.name != "nt" or executable is None:
            raise IsolationUnavailable("Требуется локальный Docker Desktop с Linux-контейнерами на Windows")
        configuration = self.root / "codex_runtime" / "docker_cli"
        configuration.mkdir(parents=True, exist_ok=True)
        (configuration / "config.json").write_text("{}", encoding="utf-8")
        return [executable, "--host", "npipe:////./pipe/docker_engine", "--config", str(configuration), *arguments]

    def docker(self, *arguments, timeout=30):
        try:
            result = subprocess.run(self.docker_command(*arguments), capture_output=True, timeout=timeout,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            raise IsolationUnavailable("Локальный Docker недоступен") from None
        if result.returncode:
            raise IsolationUnavailable("Операция изолированного Docker завершилась ошибкой")
        return result.stdout.decode("utf-8")

    def image_metadata(self):
        try:
            metadata = json.loads((self.root / "codex_runtime" / "runtime.json").read_text(encoding="utf-8"))
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", metadata["image_id"]):
                raise ValueError()
            expected = hashlib.sha256(Path(__file__).with_name("codex_worker.py").read_bytes()).hexdigest()
            if metadata["worker_sha256"] != expected:
                raise ValueError()
            if metadata.get("source_sha256") != source_fingerprint():
                raise ValueError()
            image = json.loads(self.docker("image", "inspect", metadata["image_id"]))[0]
            if image["Os"] != "linux" or image["Architecture"] != "amd64" or image["Config"].get("Volumes"):
                raise ValueError()
            return metadata
        except (OSError, ValueError, KeyError, TypeError):
            raise IsolationUnavailable("Подготовьте или обновите локальный защищённый образ Codex") from None

    def _mounts(self):
        mounts = [(str((self.profile_home / "server").resolve()), "/profile")]
        if self.workspace:
            if not self.workspace.is_dir() or self.workspace == Path(self.workspace.anchor) or self.workspace == Path.home():
                raise IsolationUnavailable("Выберите отдельную локальную папку проекта")
            if self.root.resolve().is_relative_to(self.workspace) or self.workspace.is_relative_to(self.root.resolve()):
                raise IsolationUnavailable("Каталог личных данных kid нельзя использовать как проект Codex")
            if str(self.workspace).startswith(("\\\\", "//")):
                raise IsolationUnavailable("Сетевые рабочие папки запрещены")
            import ctypes
            if ctypes.windll.kernel32.GetDriveTypeW(self.workspace.anchor) != 3:
                raise IsolationUnavailable("Требуется локальный фиксированный диск")
            mounts.append((str(self.workspace), "/workspace"))
        if self.network_scope == "tools":
            dependencies = self.root / "codex_runtime" / "deps"
            if dependencies.is_dir():
                mounts.append((str(dependencies.resolve()), "/opt/kid/deps"))
            cache = self.root / "codex_runtime" / "embed_cache"
            cache.mkdir(parents=True, exist_ok=True)
            mounts.append((str(cache.resolve()), "/embed_cache"))
            for name in ("data", "generated_files", "history", "documents"):
                source = self.root / name
                source.mkdir(parents=True, exist_ok=True)
                mounts.append((str(source.resolve()), "/tools_data/" + name))
        if any("," in source for source, _ in mounts):
            raise IsolationUnavailable("Путь с запятой не поддерживается защищённым launcher")
        return mounts

    def verify_container(self):
        value = json.loads(self.docker("inspect", self.container))[0]
        host, config = value["HostConfig"], value["Config"]
        required_mounts = {destination: os.path.normcase(source) for source, destination in self._mounts()}
        actual_mounts = {mount["Destination"]: os.path.normcase(mount["Source"]) for mount in value.get("Mounts", [])
                         if mount.get("Type") != "tmpfs"}
        if (host.get("NetworkMode") != "none" or host.get("Privileged") or not host.get("ReadonlyRootfs")
                or "ALL" not in (host.get("CapDrop") or []) or host.get("CapAdd")
                or host.get("Devices") or host.get("DeviceRequests") or host.get("PortBindings")
                or host.get("PidMode") == "host" or host.get("IpcMode") == "host"
                or host.get("LogConfig", {}).get("Type") != "none"
                or "no-new-privileges" not in (host.get("SecurityOpt") or [])
                or config.get("User") != "1000:1000" or actual_mounts != required_mounts
                or config.get("Entrypoint") != ["/usr/local/bin/python"]
                or config.get("Cmd") != ["-I", "/opt/kid/codex_worker.py"]):
            raise IsolationUnavailable("Параметры контейнера не соответствуют политике изоляции")
        if any(mount["Destination"] == "/opt/kid/deps" and mount.get("RW") for mount in value.get("Mounts", [])):
            raise IsolationUnavailable("Зависимости инструментов должны быть доступны только для чтения")
        networks = value.get("NetworkSettings", {}).get("Networks") or {}
        if set(networks) - {"none"}:
            raise IsolationUnavailable("Обнаружен внешний сетевой интерфейс")

    def start_worker(self):
        self.profile_home.mkdir(parents=True, exist_ok=True)
        (self.profile_home / "server").mkdir(parents=True, exist_ok=True)
        metadata = self.image_metadata()
        arguments = ["create", "--rm", "--interactive", "--pull", "never", "--network", "none", "--read-only", "--cap-drop", "ALL",
                     "--security-opt", "no-new-privileges", "--user", "1000:1000", "--pids-limit", "256",
                     "--memory", "2g", "--cpus", "2", "--no-healthcheck", "--log-driver", "none",
                     "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m",
                     "--tmpfs", "/home/codex:rw,nosuid,nodev,uid=1000,gid=1000,mode=0700,size=16m"]
        arguments.extend(["--tmpfs", "/tools_data:rw,nosuid,nodev,uid=1000,gid=1000,mode=0700,size=64m"])
        for source, destination in self._mounts():
            readonly = ",readonly" if destination == "/opt/kid/deps" else ""
            arguments.extend(["--mount", f"type=bind,source={source},destination={destination}{readonly}"])
        arguments.extend(["--entrypoint", "/usr/local/bin/python", metadata["image_id"], "-I", "/opt/kid/codex_worker.py"])
        try:
            self.container = self.docker(*arguments).strip()
            if not re.fullmatch(r"[a-f0-9]{64}", self.container):
                raise IsolationUnavailable("Некорректный идентификатор контейнера")
            self.verify_container()  # before starting ANY container process
            self.process = subprocess.Popen(self.docker_command("start", "--attach", "--interactive", self.container),
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            text=True, encoding="utf-8", bufsize=1,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            threading.Thread(target=self._read, daemon=True).start()
            threading.Thread(target=self._heartbeat, daemon=True).start()
            event = self.events.get(timeout=20)
            if event.get("kind") != "ready":
                raise IsolationUnavailable("Изолированный worker не прошёл проверку")
            report = event.get("report", {})
            if (report.get("interfaces") != ["lo"] or report.get("child_direct_checks_blocked") != 4
                    or report.get("capabilities") != 0 or not report.get("no_new_privileges") or not report.get("root_read_only")):
                raise IsolationUnavailable("Не подтверждена блокировка прямого трафика")
            self.report, self.proxy_port, self.tool_port = report, event.get("proxy_port"), event.get("tool_port")
            self.verify_container()  # after startup, before allowing App Server
            return report
        except IsolationUnavailable:
            self.close()
            raise
        except Exception:
            self.close()
            raise IsolationUnavailable("Запуск заблокирован: изоляция не подтверждена") from None

    def start_server(self, search):
        if self.report is None or self.stopped.is_set():
            raise IsolationUnavailable("Worker не проверен")
        self.send({"kind": "start", "search": search})
        if self.events.get(timeout=20).get("kind") != "started":
            raise IsolationUnavailable("App Server не запустился")

    def _heartbeat(self):
        while not self.stopped.wait(5):
            try:
                self.send({"kind": "heartbeat"})
            except Exception:
                self.close()
                return

    def send(self, frame):
        if self.process is None or self.stopped.is_set():
            raise ProxyUnavailable()
        encoded = json.dumps(frame, separators=(",", ":"))
        if len(encoded) > MAX_FRAME:
            raise IsolationUnavailable("Превышен размер сообщения worker")
        with self.write_lock:
            try:
                self.process.stdin.write(encoded + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError):
                raise ProxyUnavailable() from None

    def _dial(self, frame):
        identity = frame["id"]
        connection = None
        try:
            allowed = service_destination(frame.get("host"), frame.get("port"))
            if self.network_scope == "tools":
                host = str(frame.get("host") or "").lower()
                allowed = frame.get("port") in (80, 443) and not host.endswith((".local", ".internal")) and host not in ("localhost", "host.docker.internal")
                try:
                    allowed = allowed and ipaddress.ip_address(host).is_global
                except ValueError:
                    pass
            if not allowed:
                self.send({"kind": "dial_result", "id": identity, "ok": False})
                return
            connection = connect_via_proxy(self.endpoint, frame["host"], frame["port"])
            connection.settimeout(120)
            with self.lock:
                if self.stopped.is_set():
                    return
                self.tunnels[identity] = connection
            self.send({"kind": "dial_result", "id": identity, "ok": True})
            while not self.stopped.is_set():
                data = connection.recv(32768)
                if not data:
                    break
                self.send({"kind": "data", "id": identity, "data": base64.b64encode(data).decode("ascii")})
        except Exception:
            with self.lock:
                locally_closed = connection is not None and self.tunnels.get(identity) is not connection
            if not self.stopped.is_set() and not locally_closed:
                self.failure = ProxyUnavailable()
                threading.Thread(target=self.close, daemon=True).start()
        finally:
            if connection:
                connection.close()
            with self.lock:
                self.tunnels.pop(identity, None)
                self.pending_dials.discard(identity)
            if not self.stopped.is_set():
                try:
                    self.send({"kind": "close", "id": identity})
                except Exception:
                    pass

    def _read(self):
        try:
            while not self.stopped.is_set():
                line = self.process.stdout.readline(MAX_FRAME + 1)
                if not line or len(line) > MAX_FRAME:
                    break
                frame = json.loads(line)
                kind = frame.get("kind")
                if kind == "server":
                    self.reader.put(frame["message"])
                elif kind in ("ready", "started", "fatal", "tool_executed"):
                    self.events.put_nowait(frame)
                    if kind == "fatal":
                        break
                elif kind in ("tool_call", "model_call"):
                    threading.Thread(target=self._answer_tool, args=(frame,), daemon=True).start()
                elif kind == "dial":
                    identity = frame.get("id")
                    with self.lock:
                        if not isinstance(identity, str) or identity in self.pending_dials or len(self.pending_dials) >= 32:
                            raise IsolationUnavailable("Некорректный канал прокси")
                        self.pending_dials.add(identity)
                    threading.Thread(target=self._dial, args=(frame,), daemon=True).start()
                elif kind in ("data", "close"):
                    with self.lock:
                        connection = self.tunnels.get(frame.get("id"))
                    if connection:
                        if kind == "data":
                            connection.sendall(base64.b64decode(frame["data"], validate=True))
                        else:
                            with self.lock:
                                self.tunnels.pop(frame.get("id"), None)
                            try:
                                connection.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                            connection.close()
                elif kind == "server_closed":
                    break
                else:
                    raise IsolationUnavailable("Неизвестное сообщение worker")
        except Exception:
            pass
        finally:
            try:
                self.reader.put(None)
                self.events.put_nowait({"kind": "fatal"})
            except Exception:
                pass
            if not self.stopped.is_set():
                threading.Thread(target=self.close, daemon=True).start()

    def _answer_tool(self, frame):
        try:
            result = self.on_tool(frame) if self.on_tool else {"isError": True, "content": [{"type": "text", "text": "Инструмент не подключён"}]}
            self.send({"kind": "tool_result", "id": frame["id"], "result": result})
        except Exception:
            try:
                self.send({"kind": "tool_result", "id": frame["id"], "result": {"error": "Возможность недоступна"}})
            except Exception:
                pass

    def close(self):
        if self.stopped.is_set():
            return
        self.stopped.set()
        with self.lock:
            connections = list(self.tunnels.values())
            self.tunnels.clear()
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        if self.container:
            try:
                self.docker("rm", "--force", self.container, timeout=15)
            except Exception:
                pass
        if self.process:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        try:
            self.reader.put(None)
        except Exception:
            pass
