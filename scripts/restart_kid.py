"""Restart only this kid installation, using a detached launcher."""
import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import psutil

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "WEB_UI.pyw"
PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
ENTRIES = {os.path.normcase(str(ROOT / name)) for name in ("WEB_UI.pyw", "desktop_app.py")}
LOG = Path(os.environ["APPDATA"]) / "kid" / "restart.log"


def instances():
    result = []
    for process in psutil.process_iter(["name", "cmdline", "ppid"]):
        try:
            if not (process.info["name"] or "").lower().startswith("python"):
                continue
            if any(os.path.normcase(arg) in ENTRIES for arg in (process.info["cmdline"] or [])[1:]):
                result.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return result


def log(message):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")


def close_windows(pids):
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

    @callback_type
    def visit(window, _):
        process_id = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(process_id))
        if process_id.value in pids:
            title = ctypes.create_unicode_buffer(512)
            user.GetWindowTextW(window, title, len(title))
            if title.value in ("Assistant", "Sprut AI Assistant", "kid"):
                user.PostMessageW(window, 0x0010, 0, 0)  # WM_CLOSE: normal application cleanup.
        return True
    user.EnumWindows(visit, 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay", type=int, default=15)
    parser.add_argument("--check", action="store_true", help="Validate paths and identify kid without stopping it.")
    args = parser.parse_args()
    if not 0 <= args.delay <= 600:
        parser.error("delay must be between 0 and 600 seconds")
    if not PYTHONW.is_file() or not ENTRY.is_file():
        raise RuntimeError("kid launcher or virtual-environment pythonw.exe is missing")
    current = instances()
    pids = {process.pid for process in current}
    roots = [process for process in current if process.info["ppid"] not in pids]
    if len(roots) > 1:
        raise RuntimeError("Multiple independent kid instances found; refusing to stop them automatically")
    if args.check:
        print(json.dumps({"root": str(ROOT), "process_ids": sorted(pids), "root_process_ids": [p.pid for p in roots], "valid": True}))
        return

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    name = "Local\\KidRestart-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:24]
    mutex = kernel.CreateMutexW(None, False, name)
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(mutex)
        return
    try:
        log(f"Restart queued, delay={args.delay}s")
        time.sleep(args.delay)
        current = instances()
        pids = {process.pid for process in current}
        roots = [process for process in current if process.info["ppid"] not in pids]
        if len(roots) > 1:
            raise RuntimeError("Multiple kid instances appeared; restart cancelled")
        close_windows(pids)
        deadline = time.monotonic() + 20
        while instances() and time.monotonic() < deadline:
            time.sleep(0.25)
        for process in roots:
            if process.is_running():
                subprocess.run(["taskkill.exe", "/F", "/T", "/PID", str(process.pid)],
                               capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        if instances():
            raise RuntimeError("Previous kid instance is still running; new launch refused")
        time.sleep(1)
        child = subprocess.Popen([str(PYTHONW), str(ENTRY)], cwd=str(ROOT),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        log(f"kid launched, PID={child.pid}")
    finally:
        kernel.CloseHandle(mutex)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"ERROR: {error}")
        raise
