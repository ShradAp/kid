"""Owned HTTP/Next.js resources and idempotent desktop application shutdown."""

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime


def wait_for_server(host: str, port: int, timeout: int = 60) -> bool:
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except (OSError, ConnectionRefusedError):
            time.sleep(0.5)
    return False


def _write_startup_error(message: str) -> None:
    """Последняя ошибка старта — в файл рядом с конфигом: pythonw молчит,
    а без этого файла невидимый краш нельзя было диагностировать."""
    try:
        from config_manager import CONFIG_DIR

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        path = CONFIG_DIR / "last_error.log"
        stamp = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        path.write_text(f"[{stamp}] {message}\n", encoding="utf-8")
    except Exception:
        pass


class _BridgeHTTPServer(http.server.ThreadingHTTPServer):
    # HTTPServer включает SO_REUSEADDR; на Windows это допускает два моста
    # на одном порту. Эксклюзивная привязка исключает такое смешение запросов.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class ApplicationLifecycle:
    def __init__(self, frontend_dir, user_data_dir, log):
        self.frontend_dir = frontend_dir
        self.user_data_dir = user_data_dir
        self.log = log
        self.backend_server = None
        self.frontend_process = None
        self.bridge_started = threading.Event()
        self.shutting_down = threading.Event()
        self.backend_lock = threading.Lock()
        self.frontend_lock = threading.Lock()
        self.cleanup_lock = threading.Lock()

    def start_backend(self, handler, bridge_port):
        try:
            server = _BridgeHTTPServer(("127.0.0.1", bridge_port), handler)
        except OSError as e:
            self.bridge_started.set()
            _write_startup_error(
                f"Порт {bridge_port} уже занят: {e}. Скорее всего, уже работает "
                "другой экземпляр приложения — закройте его (трей → Выход) и запустите заново."
            )
            return
        with self.backend_lock:
            if self.shutting_down.is_set():
                server.server_close()
                return
            self.backend_server = server
        self.log(f"[*] Python Bridge запущен на порту {bridge_port}")
        self.bridge_started.set()
        server.serve_forever()


    def start_frontend(self, port, bridge_port, bridge_token):
        # CREATE_NO_WINDOW = 0x08000000 гарантирует отсутствие черных окон консоли
        creationflags = 0x08000000 if os.name == "nt" else 0
        # Продакшн-сборка стартует заметно быстрее dev-сервера: `next start` поднимается
        # ~за 1 с и отдаёт уже скомпилированные страницы, без компиляции на лету (именно
        # она в dev давала задержку на первом GET). Dev-режим можно принудительно вернуть
        # переменной окружения KID_FRONTEND_DEV=1 — например, во время правок интерфейса
        # (после правок фронтенда нужен `npm run build`, чтобы продакшн-сборка обновилась).
        use_prod = (
            not os.environ.get("KID_FRONTEND_DEV")
            and os.path.isfile(os.path.join(self.frontend_dir, ".next", "BUILD_ID"))
        )
        script = "start" if use_prod else "dev"
        self.log(f"[*] Запуск фронтенда: next {script} (порт {port})")
        try:
            # pythonw (GUI-подсистема) не передаёт корректно PATH в cmd.exe, поэтому
            # `npm.cmd run ... shell=True` из pythonw падает с «npm.cmd не распознан».
            # Запускаем Next.js напрямую через node.exe (абсолютный путь, без оболочки).
            node_exe = shutil.which("node.exe")
            runtime_state = os.path.join(os.path.dirname(self.frontend_dir), '.install-state.json')
            if os.path.isfile(runtime_state):
                with open(runtime_state, encoding='utf-8-sig') as stream:
                    installed = json.load(stream)
                if installed.get('complete') is True:
                    node_exe = os.path.join(installed['node'], 'node.exe')
            next_bin = os.path.join(self.frontend_dir, "node_modules", "next", "dist", "bin", "next")
            if not node_exe or not os.path.isfile(next_bin):
                raise RuntimeError(f"node.exe или Next.js CLI не найдены (node={node_exe!r})")
            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            with self.frontend_lock:
                if self.shutting_down.is_set():
                    return
                with open(self.user_data_dir / "frontend.log", "a", encoding="utf-8", errors="replace") as frontend_log:
                    self.frontend_process = subprocess.Popen(
                        [node_exe, next_bin, script, "-p", str(port), "--hostname", "127.0.0.1"],
                        env={**os.environ, "KID_BRIDGE_TOKEN": bridge_token, "KID_BRIDGE_PORT":str(bridge_port)},
                        cwd=self.frontend_dir,
                        creationflags=creationflags,
                        stdout=frontend_log,
                        stderr=frontend_log,
                    )
        except Exception as exc:
            self.log(f"[!] Не удалось запустить фронтенд: {exc}")
            self.frontend_process = None


    def shutdown(self, codex_service, cancel_events, unhook_hotkeys) -> bool:
        """Однократная очистка при закрытии окна, выходе из трея и ошибке старта."""
        if not self.cleanup_lock.acquire(blocking=False):
            return False
        try:
            if self.shutting_down.is_set():
                return False
            self.shutting_down.set()
            self.log("[*] Завершение приложения: остановка запросов и дочерних процессов")
            for cancel_event in list(cancel_events.values()):
                cancel_event.set()
            codex_service.close()
            try:
                unhook_hotkeys()
            except Exception:
                pass
            with self.backend_lock:
                server, self.backend_server = self.backend_server, None
            if server is not None:
                try:
                    server.shutdown()
                except Exception as exc:
                    self.log(f"[!] Остановка HTTP-моста: {exc}")
                try:
                    server.server_close()
                except Exception as exc:
                    self.log(f"[!] Закрытие HTTP-сокета: {exc}")
            network_module = sys.modules.get("network_agent.core")
            agent = getattr(network_module, "agent_core", None)
            if agent is not None:
                try:
                    agent.close()
                except Exception as exc:
                    self.log(f"[!] Остановка браузера поиска: {exc}")
            with self.frontend_lock:
                process, self.frontend_process = self.frontend_process, None
            if process is not None:
                try:
                    if process.poll() is None:
                        if os.name == "nt":
                            try:
                                subprocess.run(
                                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                                    capture_output=True, timeout=5, creationflags=0x08000000,
                                )
                            except (OSError, subprocess.TimeoutExpired) as exc:
                                self.log(f"[!] Завершение дерева Next.js: {exc}")
                        else:
                            process.terminate()
                        if process.poll() is None:
                            process.kill()
                    process.wait(timeout=5)
                except Exception as exc:
                    self.log(f"[!] Остановка Next.js: {exc}")
            return True
        finally:
            self.cleanup_lock.release()


