"""Run bounded local workers and terminate their owned child processes on timeout."""
import json
import os
import signal
import subprocess
import sys


def run_worker(script, payload, timeout):
    child = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt")
    try:
        stdout, _ = child.communicate(json.dumps(payload), timeout=max(.1, timeout))
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if child.poll() is None:
            child.kill()
        child.communicate()
        raise
    if child.returncode:
        return {}
    # Only the worker's small JSON result is used; SDK/driver stderr stays private.
    if len(stdout) > 100_000:
        return {}
    result = json.loads(stdout)
    return result if isinstance(result, dict) else {}
