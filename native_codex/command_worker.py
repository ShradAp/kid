"""Host command worker: parent assigns its kill-on-close job before input."""
import ctypes
import json
from pathlib import Path
import subprocess
import sys
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    # No child exists until the parent has assigned our Windows job and sent
    # this request. A parent crash closes stdin and the job, failing closed.
    request = json.load(sys.stdin)
    system = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetSystemDirectoryW(system, len(system)):
        return 1
    if request["shell"] == "cmd":
        argv = [str(Path(system.value) / "cmd.exe"), "/d", "/s", "/c", request["command"]]
    else:
        binary = Path(system.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        argv = [str(binary), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); " + request["command"]]
    return subprocess.call(argv, cwd=request["cwd"], stdin=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit(1)
