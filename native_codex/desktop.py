"""Explicit interactive terminal launches, separate from kill-on-close command jobs."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import time
import uuid


def visible_windows():
    if os.name != "nt":
        return []
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowTextLengthW.restype = ctypes.c_int
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    result = []
    @callback_type
    def collect(hwnd, _):
        if user.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(user.GetWindowTextLengthW(hwnd) + 1)
            user.GetWindowTextW(hwnd, title, len(title))
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            result.append({"hwnd": int(hwnd), "pid": pid.value, "title": title.value})
        return True
    if not user.EnumWindows(collect, 0):
        raise OSError("Не удалось проверить окна рабочего стола")
    return result


def process_image(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        image = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(image))
        return Path(image.value) if kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)) else None
    finally:
        kernel.CloseHandle(handle)


def opera_gx_executable():
    """Resolve a registered GX install, never execute a model-supplied path."""
    import winreg
    candidates = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths\opera.exe", 0, winreg.KEY_READ | view) as key:
                    value = winreg.QueryValueEx(key, "")[0]
                    candidate = Path(os.path.expandvars(str(value).strip().strip('"')))
                    if any(part.casefold().replace(" ", "") == "operagx" for part in candidate.parts):
                        candidates.append(candidate)
            except OSError:
                pass
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")):
        if base:
            root = Path(base)
            for folder in (root / "Opera GX", root / "Programs" / "Opera GX"):
                candidates.extend((folder / "opera.exe", folder / "launcher.exe"))
    for path in candidates:
        if path.is_absolute() and path.name.casefold() in ("opera.exe", "launcher.exe") and path.is_file():
            return path.resolve()
    raise ValueError("Opera GX не найдена в зарегистрированных или стандартных каталогах установки")


def open_opera_gx(cancel, timeout=15):
    if os.name != "nt":
        raise ValueError("Открытие Opera GX доступно только в Windows")
    if cancel.is_set():
        raise ValueError("Запуск отменён")
    executable = opera_gx_executable()
    before = {window["hwnd"] for window in visible_windows()}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 1
    process = subprocess.Popen([str(executable), "--new-window", "about:blank"], cwd=str(executable.parent),
                               startupinfo=startup, close_fds=True)
    deadline = time.monotonic() + min(30, max(1, timeout))
    while time.monotonic() < deadline:
        for window in visible_windows():
            if window["hwnd"] in before:
                continue
            image = process_image(window["pid"])
            if image and image.name.casefold() == "opera.exe" and image.resolve().is_relative_to(executable.parent):
                return {"ok":True, "app":"opera_gx", "pid":window["pid"], "launcher_pid":process.pid,
                        "window_visible":True, "window_handle":window["hwnd"], "executable":str(image),
                        "verification":"new_visible_window_owned_by_registered_opera_gx_executable"}
        if cancel.wait(.15):
            break
    return {"ok":False, "app":"opera_gx", "launcher_pid":process.pid, "window_visible":False,
            "reason":"Новое видимое окно Opera GX не подтверждено; не сообщайте об успешном открытии."}


def open_app(app, cancel):
    if app == "opera_gx":
        return open_opera_gx(cancel)
    return open_terminal(app, cancel)


def open_terminal(app, cancel, timeout=10):
    if app not in ("cmd", "powershell"):
        raise ValueError("Выберите cmd или powershell")
    if os.name != "nt":
        raise ValueError("Открытие терминала доступно только в Windows")
    if cancel.is_set():
        raise ValueError("Запуск отменён")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    system = ctypes.create_unicode_buffer(32768)
    if not kernel.GetSystemDirectoryW(system, len(system)):
        raise OSError("Не найден системный каталог Windows")
    marker = "KID " + app.upper() + " " + uuid.uuid4().hex[:12]
    if app == "cmd":
        argv = [str(Path(system.value) / "cmd.exe"), "/d", "/k", "title " + marker]
    else:
        argv = [str(Path(system.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
                "-NoLogo", "-NoProfile", "-NoExit", "-Command", "$Host.UI.RawUI.WindowTitle = '" + marker + "'"]
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 1  # SW_SHOWNORMAL (not exported by subprocess)
    # Intentionally no background ProcessJob: this user-requested window must
    # remain alive after the tool returns. Never accept a command or extra args.
    process = subprocess.Popen(argv, cwd=system.value, startupinfo=startup,
                               creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)
    deadline = time.monotonic() + min(20, max(1, timeout))
    reason = "Видимое окно не подтверждено. Не сообщайте пользователю, что терминал открыт."
    while time.monotonic() < deadline:
        try:
            window = next((window for window in visible_windows() if marker in window["title"]), None)
        except OSError:
            reason = "Процесс создан, но проверка окон рабочего стола недоступна. Открытие окна не подтверждено."
            break
        if window and process.poll() is None:
            return {"ok": True, "app": app, "pid": process.pid, "window_visible": True,
                    "window_handle": window["hwnd"], "title": marker,
                    "verification": "visible_top_level_window_with_unique_launch_title"}
        if process.poll() is not None or cancel.wait(.1):
            break
    return {"ok": False, "app": app, "pid": process.pid, "window_visible": False,
            "process_running": process.poll() is None, "cancelled": cancel.is_set(),
            "reason": reason}
