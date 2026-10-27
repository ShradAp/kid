"""Install Linux tool dependencies only inside the verified proxy-only worker."""
import argparse
import json
from pathlib import Path
import shutil
import os
import re
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_paths import USER_DATA_DIR
from codex_runtime import IsolatedRuntime, IsolationUnavailable
from scripts.prepare_codex_runtime import private_endpoint


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--profile-id")
    selection.add_argument("--use-app-proxy", action="store_true")
    args = parser.parse_args()
    endpoint = private_endpoint(args.profile_id, args.use_app_proxy)
    if shutil.disk_usage(USER_DATA_DIR).free < 16 * 1024**3:
        raise IsolationUnavailable("Для безопасной подготовки нужно не менее 16 ГиБ свободного места")
    target = USER_DATA_DIR / "codex_runtime" / "deps"
    if target.exists():
        raise IsolationUnavailable("Каталог зависимостей уже существует; сначала проверьте его вручную")
    with tempfile.TemporaryDirectory(prefix="kid-tool-deps-") as directory:
        root = Path(directory)
        (root / "codex_runtime").mkdir()
        shutil.copyfile(USER_DATA_DIR / "codex_runtime" / "runtime.json", root / "codex_runtime" / "runtime.json")
        runtime = IsolatedRuntime(root, root / "worker", endpoint, network_scope="tools")
        finished = threading.Event()
        budget_exceeded = threading.Event()
        try:
            runtime.start_worker()
            def monitor_storage():
                while not finished.wait(2):
                    try:
                        size = sum(path.stat().st_size for path in (root / "worker").rglob("*") if path.is_file())
                        if size > 2 * 1024**3 or shutil.disk_usage(root).free < 12 * 1024**3:
                            budget_exceeded.set()
                            runtime.close()
                            return
                    except OSError:
                        continue
            threading.Thread(target=monitor_storage, daemon=True).start()
            proxy = f"http://127.0.0.1:{runtime.proxy_port}"
            # No resolver, installer or subprocess has an external interface.
            # PyPI, wheels and their redirects go via the stdio broker.
            packages = ["lancedb==0.40.0", "fastembed==0.9.0", "pyarrow==26.0.0",
                        "trafilatura==2.3.1", "curl_cffi==0.16.3", "yt-dlp==2026.10.21"]
            runtime.docker("exec", runtime.container, "/usr/local/bin/python", "-I", "-c",
                           "from pathlib import Path; Path('/profile/pip-tmp').mkdir()")
            command = runtime.docker_command("exec", "--env", "TMPDIR=/profile/pip-tmp", runtime.container, "/usr/local/bin/python", "-I", "-m", "pip", "install",
                                             "--disable-pip-version-check", "--no-cache-dir", "--only-binary=:all:",
                                             "--index-url", "https://pypi.org/simple/", "--proxy", proxy,
                                             "--target", "/profile/deps", *packages)
            outcome = subprocess.run(command, capture_output=True, timeout=1200,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if outcome.returncode:
                # The container never receives upstream credentials. Still
                # report only pip's short ERROR lines, stripping URLs.
                for line in outcome.stderr.decode("utf-8", errors="replace").splitlines():
                    if line.startswith("ERROR:"):
                        print(re.sub(r"https?://\S+", "[source]", line)[:400])
                raise IsolationUnavailable("Установка зависимостей в изолированном окружении не завершилась")
            if budget_exceeded.is_set():
                raise IsolationUnavailable("Подготовка остановлена по ограничению диска")
            installed = root / "worker" / "server" / "deps"
            if not (installed / "lancedb").is_dir() or not (installed / "fastembed").is_dir():
                raise IsolationUnavailable("Не удалось подготовить память в защищённом окружении")
            # Destination is explicit and checked. No recursive removal/move.
            if target.resolve().parent != (USER_DATA_DIR / "codex_runtime").resolve():
                raise IsolationUnavailable("Неверный каталог зависимостей")
            shutil.copytree(installed, target)
            print(json.dumps({"tool_dependencies_prepared": True, "downloads_proxy_only": True}))
        finally:
            finished.set()
            runtime.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, IsolationUnavailable) else "Подготовка инструментов заблокирована")
        raise SystemExit(1)
