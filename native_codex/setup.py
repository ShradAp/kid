"""One-time preparation from the ordinary .pyw launch; UAC only for WFP."""
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import subprocess
import sys
import threading

from codex_engine import CodexBlocked
from codex_proxy import ProxyEndpoint
from .broker import BROKER_PORT
from .process import metadata
from .wfp import ProtectionUnavailable, verify

_lock = threading.Lock()


def elevate_setup(root):
    class ExecuteInfo(C.Structure):
        _fields_ = [("size", W.DWORD), ("mask", W.ULONG), ("hwnd", W.HWND), ("verb", W.LPCWSTR),
                    ("file", W.LPCWSTR), ("parameters", W.LPCWSTR), ("directory", W.LPCWSTR),
                    ("show", C.c_int), ("instance", W.HINSTANCE), ("id_list", C.c_void_p),
                    ("class_name", W.LPCWSTR), ("class_key", W.HKEY), ("hotkey", W.DWORD),
                    ("icon", W.HANDLE), ("process", W.HANDLE)]
    script = Path(__file__).resolve().parents[1] / "scripts" / "prepare_native_codex.py"
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    output = Path(root) / "codex_native" / "setup-result.json"
    output.unlink(missing_ok=True)
    info = ExecuteInfo()
    info.size, info.mask, info.verb, info.show = C.sizeof(info), 0x140, "runas", 0
    info.file = str(python)
    info.parameters = subprocess.list2cmdline([str(script), "--install-protection", "--result-file", str(output)])
    info.directory = str(script.parents[1])
    shell = C.WinDLL("shell32", use_last_error=True)
    shell.ShellExecuteExW.argtypes, shell.ShellExecuteExW.restype = [C.POINTER(ExecuteInfo)], W.BOOL
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    if not shell.ShellExecuteExW(C.byref(info)):
        raise ProtectionUnavailable("Защита Windows не установлена: подтверждение администратора отменено")
    try:
        if kernel.WaitForSingleObject(info.process, 180000) != 0:
            raise ProtectionUnavailable("Установка защиты Windows не завершена; Codex заблокирован")
    finally:
        kernel.CloseHandle(info.process)
    try:
        result = json.loads(output.read_text(encoding="utf-8"))
        if result.get("ok") is not True:
            raise ValueError()
    except (OSError, ValueError, AttributeError):
        raise ProtectionUnavailable("Не удалось установить защиту Windows; Codex заблокирован") from None


def ensure(root, profile_id, profile):
    options = profile.get("codex") or {}
    ProxyEndpoint.parse(options.get("proxy", ""), options.get("proxy_password", ""))
    with _lock:
        try:
            try:
                metadata(root, verify_hashes=False)
            except ProtectionUnavailable:
                from scripts.prepare_native_codex import prepare
                prepare(profile_id, root)
            try:
                verify(Path(root) / "codex_native" / "codex.exe", BROKER_PORT)
                verify(Path(root) / "codex_native" / "network-probe.exe", BROKER_PORT)
                verify(Path(root) / "codex_native" / "codex-code-mode-host.exe", BROKER_PORT)
            except ProtectionUnavailable:
                elevate_setup(root)
                verify(Path(root) / "codex_native" / "codex.exe", BROKER_PORT)
                verify(Path(root) / "codex_native" / "network-probe.exe", BROKER_PORT)
                verify(Path(root) / "codex_native" / "codex-code-mode-host.exe", BROKER_PORT)
        except ProtectionUnavailable as error:
            raise CodexBlocked(str(error)) from None
