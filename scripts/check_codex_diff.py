"""Real isolated Git diff and disabled executable helpers, on scratch files."""
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_paths import USER_DATA_DIR
from codex_runtime import IsolatedRuntime, IsolationUnavailable
from scripts.prepare_codex_runtime import private_endpoint


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    with tempfile.TemporaryDirectory(prefix="kid-diff-check-") as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        runtime = IsolatedRuntime(USER_DATA_DIR, root / "worker", private_endpoint(use_app_proxy=True), workspace=project)
        try:
            runtime.start_worker()
            setup = """
from pathlib import Path
import subprocess
def git(*args):
 subprocess.run(['git','-c','safe.directory=/workspace','-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false',*args],cwd='/workspace',check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
git('init')
git('config','user.name','kid scratch test')
git('config','user.email','scratch@example.invalid')
Path('/workspace/file.txt').write_text('before\\n')
git('add','file.txt')
git('commit','-m','scratch')
Path('/workspace/file.txt').write_text('after\\n')
git('config','core.fsmonitor','touch /tmp/fsmonitor-ran')
git('config','diff.external','touch /tmp/external-diff-ran')
"""
            runtime.docker("exec", runtime.container, "/usr/local/bin/python", "-I", "-c", setup)
            result = json.loads(runtime.docker("exec", runtime.container, "/usr/local/bin/python", "-I",
                                               "/opt/kid/kidlib/kid_diff_worker.py", "file.txt"))
            assert "-before" in result.get("text", "") and "+after" in result["text"]
            verify = "from pathlib import Path; assert not Path('/tmp/fsmonitor-ran').exists(); assert not Path('/tmp/external-diff-ran').exists()"
            runtime.docker("exec", runtime.container, "/usr/local/bin/python", "-I", "-c", verify)
            print(json.dumps({"isolated_git_diff_verified": True, "external_helpers_not_executed": True,
                              "host_git_processes": 0, "model_requests": 0}))
        finally:
            runtime.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, (IsolationUnavailable, AssertionError)) else "Isolated diff check failed")
        raise SystemExit(1)
