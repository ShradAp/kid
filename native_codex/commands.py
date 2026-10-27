"""Confirmed host Windows commands with finite output, time and cancellation."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from code_workspace import CodeWorkspace
from .job import ProcessJob


def run(command, shell, cwd, cancel, timeout=120):
    if os.name != "nt" or shell not in ("powershell", "cmd"):
        raise ValueError("Выберите PowerShell или CMD в Windows")
    if not isinstance(command, str) or not command.strip() or len(command) > 64000 or "\x00" in command:
        raise ValueError("Некорректная команда Windows")
    folder = CodeWorkspace().grant("command", cwd)
    if cancel.is_set():
        raise ValueError("Команда отменена")
    deadline = time.monotonic() + min(300, max(1, timeout))
    with tempfile.TemporaryFile() as output:
        job = ProcessJob()
        process = subprocess.Popen([sys.executable, "-I", str(Path(__file__).with_name("command_worker.py"))],
            stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        stopped = False
        try:
            job.assign(process)  # Worker cannot spawn a command before input.
            process.stdin.write(json.dumps({"command": command, "shell": shell, "cwd": folder}).encode("utf-8"))
            process.stdin.close()
            while process.poll() is None:
                if cancel.wait(.05) or time.monotonic() >= deadline or os.fstat(output.fileno()).st_size > 512 * 1024:
                    stopped = True
                    job.terminate()
                    break
            process.wait(timeout=5)
            output.seek(0)
            raw = output.read(512 * 1024)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            job.terminate()
            job.close()
    encoding = "utf-8" if shell == "powershell" else "cp" + str(ctypes.windll.kernel32.GetOEMCP())
    return {"ok": process.returncode == 0 and not stopped, "platform": "Windows", "command": command,
            "shell": shell, "cwd": folder, "stdout": raw.decode(encoding, errors="replace"),
            "exit_code": process.returncode, "cancelled": cancel.is_set(), "stopped_by_budget": stopped}
