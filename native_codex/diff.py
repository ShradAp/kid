"""Read-only Windows Git diff with external helpers and lazy network disabled."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from code_workspace import CodeWorkspace


def project_diff(root, relative):
    check = CodeWorkspace()
    root = check.grant("diff", root)
    check.file("diff", relative)
    binary = shutil.which("git.exe")
    if not binary:
        raise ValueError("Для diff нужен установленный Git")
    env = dict(os.environ, GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull)
    args = [binary, "-c", "safe.directory=" + root, "-c", "protocol.allow=never", "-c", "core.fsmonitor=false",
            "--no-pager", "diff", "HEAD", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all", "--", relative]
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(args, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.DEVNULL, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise ValueError("Diff требует Git-репозиторий с HEAD")
        output.seek(0)
        raw = output.read(512 * 1024 + 1)
    return {"text": raw[:512*1024].decode("utf-8", errors="replace"), "truncated": len(raw) > 512*1024,
            "description": "Git diff к HEAD в выбранной папке Windows"}
