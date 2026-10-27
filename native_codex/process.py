"""Protected Windows App Server; no unprotected or Docker fallback."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import re

from codex_engine import PROTOCOL_VERSION
from codex_proxy import ProxyUnavailable
from .broker import BROKER_PORT, shared_broker
from .probe import verify_transports
from .wfp import ProtectionUnavailable, verify
from .job import ProcessJob


def metadata(root, verify_hashes=True):
    directory = Path(root) / "codex_native"
    try:
        saved = json.loads((directory / "runtime.json").read_text(encoding="utf-8"))
        if saved.get("version") != PROTOCOL_VERSION or saved.get("broker_port") != BROKER_PORT:
            raise ValueError()
        # Paths are fixed by us. Untrusted JSON cannot choose a system/global
        # codex.exe, a Python interpreter or an alternate downloaded payload.
        python_dll = saved.get("python_dll", "python312.dll")
        if not isinstance(python_dll, str) or not re.fullmatch(r"python3\d{1,2}\.dll", python_dll):
            raise ValueError()
        for name in ("codex.exe", "network-probe.exe", python_dll):
            target = directory / name
            if target.is_symlink() or target.is_junction():
                raise ValueError()
            if verify_hashes and hashlib.sha256(target.read_bytes()).hexdigest() != saved["sha256"][name]:
                raise ValueError()
            if not target.is_file():
                raise ValueError()
        return saved
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ProtectionUnavailable("Компоненты Windows Codex не подготовлены или изменены") from None


def environment(profile_home, local_proxy):
    # Only OS process prerequisites, no inherited API keys, global proxies,
    # CODEX_HOME/config, custom endpoints, browser or credential helpers.
    allowed = ("SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA")
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    result["PATH"] = os.pathsep.join((str(system / "System32"), str(system)))
    result["CODEX_HOME"] = str(Path(profile_home) / "native_server")
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        result[key] = local_proxy
    # No loopback exemptions: model HTTP is always brokered. MCP uses stdio.
    result["NO_PROXY"], result["no_proxy"] = "", ""
    result["RUST_LOG"] = "off"
    return result


def command(executable, search):
    if search not in ("codex", "kid"):
        raise ProtectionUnavailable("Неизвестный поисковый маршрут")
    result = [str(executable), "app-server", "--listen", "stdio://",
              "-c", 'cli_auth_credentials_store="file"', "-c", 'forced_login_method="chatgpt"',
              "-c", 'analytics.enabled=false', "-c", 'feedback.enabled=false',
              "-c", 'check_for_update_on_startup=false',
              "-c", 'web_search="live"' if search == "codex" else 'web_search="disabled"']
    # Executable policy does not follow arbitrary Windows descendants. Host
    # actions go through kid's confirmed executor. Browser/auth helpers,
    # plugins and installation are disabled. The official code-mode host has
    # its own identical verified WFP policy before it can be used by MCP.
    for feature in ("system_proxy_fallback", "unbounded_connection_retries", "apps", "browser_use",
                    "browser_use_external", "browser_use_full_cdp_access", "computer_use", "remote_plugin",
                    "skill_mcp_dependency_install", "daemon_auto_start", "shell_tool", "unified_exec",
                    "code_mode"):
        result += ["--disable", feature]
    result += ["--enable", "code_mode_host"]
    return result


class ProtectedProcess:
    def __init__(self, root, profile_home, endpoint, workspace=None):
        self.root, self.home = Path(root), Path(profile_home)
        self.endpoint, self.workspace = endpoint, workspace
        self.process = None
        self.failure = None
        self.report = None
        self.stopped = threading.Event()
        self.broker, self.authorization = None, None
        self.close_lock = threading.RLock()
        self.job = None

    def start(self, search):
        if self.process is not None or self.stopped.is_set():
            raise ProtectionUnavailable("Повторный запуск закрытого транспорта запрещён")
        if os.name != "nt":
            raise ProtectionUnavailable("Требуется Windows")
        saved = metadata(self.root)
        directory = self.root / "codex_native"
        executable = directory / "codex.exe"
        # ALL gates precede broker registration and Popen, including local
        # protocol initialization: Codex can refresh tokens during startup.
        policy = verify(executable, BROKER_PORT)
        helper = directory / "codex-code-mode-host.exe"
        if hashlib.sha256(helper.read_bytes()).hexdigest() != saved["sha256"]["codex-code-mode-host.exe"]:
            raise ProtectionUnavailable("Компонент инструментов Codex изменён")
        verify(helper, BROKER_PORT)
        proof = verify_transports(directory / "network-probe.exe", saved["python_home"], BROKER_PORT)
        native_home = self.home / "native_server"
        native_home.mkdir(parents=True, exist_ok=True)
        previous_auth = self.home / "server" / "auth.json"
        if not (native_home / "auth.json").exists() and previous_auth.is_file():
            # Linux and Windows SQLite state must not be opened concurrently.
            # Import only the saved account, never mutate the former runtime.
            with (native_home / "auth.json").open("xb") as target:
                target.write(previous_auth.read_bytes())
        working = Path(self.workspace) if self.workspace else self.home / "working"
        working.mkdir(parents=True, exist_ok=True)
        self.broker = shared_broker()
        self.authorization, proxy = self.broker.register(self.endpoint, self._failed)
        try:
            self.job = ProcessJob()
            self.process = subprocess.Popen(command(executable, search), cwd=working,
                env=environment(self.home, proxy), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.job.assign(self.process)
            self.report = {"policy": policy, "proof": proof, "runtime": "windows-native"}
            threading.Thread(target=self._watch, daemon=True).start()
        except BaseException:
            self.close()
            raise
        return self.process

    def _watch(self):
        while not self.stopped.wait(1):
            try:
                if self.process.poll() is not None:
                    raise ProtectionUnavailable("Процесс Codex завершился")
                verify(self.root / "codex_native" / "codex.exe", BROKER_PORT)
                verify(self.root / "codex_native" / "codex-code-mode-host.exe", BROKER_PORT)
            except Exception:
                self._failed()
                return

    def _failed(self):
        self.failure = ProxyUnavailable()
        self.close()

    def close(self):
        with self.close_lock:
            self._close()

    def _close(self):
        if self.stopped.is_set():
            return
        self.stopped.set()
        if self.broker and self.authorization:
            self.broker.unregister(self.authorization)
            self.authorization = None
        if self.process and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        if self.job:
            self.job.close()
            self.job = None
        # Persistent WFP filters are intentionally NOT removed here.
