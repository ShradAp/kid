"""Bounded Git diff, invoked ONLY inside the verified network-none worker."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main(relative):
    root = Path("/workspace")
    target = (root / relative).resolve(strict=True)
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError()
    env = dict(os.environ, GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL="/dev/null")
    command = ["git", "-c", "safe.directory=/workspace", "-c", "protocol.allow=never", "-c", "core.fsmonitor=false",
               "--no-pager", "diff", "HEAD", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all", "--", relative]
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(command, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.DEVNULL, timeout=10)
        if result.returncode:
            raise ValueError()
        output.seek(0)
        raw = output.read(512 * 1024 + 1)
    return {"text": raw[:512 * 1024].decode("utf-8", errors="replace"), "truncated": len(raw) > 512 * 1024,
            "description": "Изолированный Git diff к HEAD; новые неотслеживаемые файлы смотрите как текст"}


if __name__ == "__main__":
    try:
        print(json.dumps(main(sys.argv[1]), ensure_ascii=False))
    except Exception:
        print(json.dumps({"error": "Diff недоступен: требуется локальный Git-репозиторий с HEAD и доступными объектами"}))
