"""Check Linux memory dependencies with disposable facts and an offline model."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_paths import USER_DATA_DIR
from codex_runtime import IsolatedRuntime, IsolationUnavailable
from scripts.prepare_codex_runtime import private_endpoint


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    # Existing FastEmbed cache is public model data, not user facts. Reuse it
    # without downloads; preserve files already prepared in the target cache.
    source = Path(os.environ["LOCALAPPDATA"]) / "kid" / "fastembed"
    target = USER_DATA_DIR / "codex_runtime" / "embed_cache"
    if source.is_dir():
        size = sum(path.stat().st_size for path in source.rglob("*") if path.is_file())
        if size > 800 * 1024**2 or shutil.disk_usage(target.parent).free < 16 * 1024**3:
            raise IsolationUnavailable("Копирование модели не прошло ограничение диска")
        for path in source.rglob("*"):
            if not path.is_file() or ".locks" in path.relative_to(source).parts:
                continue
            destination = target / path.relative_to(source)
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
    endpoint = private_endpoint(use_app_proxy=True)
    with tempfile.TemporaryDirectory(prefix="kid-memory-check-") as directory:
        runtime = IsolatedRuntime(USER_DATA_DIR, Path(directory), endpoint, network_scope="tools")
        try:
            runtime.start_worker()
            code = """
import contextlib,io,json,os,sys
sys.path[:0]=['/opt/kid/deps','/opt/kid/kidlib']
os.environ.update(KID_CONFIG_DIR='/tmp/kid-memory-check', KID_EMBED_CACHE='/embed_cache', HF_HUB_OFFLINE='1')
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
 import lancedb,fastembed,trafilatura,curl_cffi
 from memory import user_profile as m
 m.set_log_callback(lambda text: None)
 backend=m.warmup()
 saved=m.remember_user_fact('Temporary isolated smoke fact', 'preferences')
 info=m.status()
 assert saved.get('ok'), 'scratch memory write failed'
 assert info.get('storage') == 'lancedb', 'native database not loaded'
print(json.dumps({'linux_dependencies_imported':True,'scratch_memory_saved':True,'storage':info.get('storage'),'embedding_backend':backend,'model_downloads':0,'user_facts_touched':False}))
"""
            print(runtime.docker("exec", runtime.container, "/usr/local/bin/python", "-I", "-c", code, timeout=90).strip())
        finally:
            runtime.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, IsolationUnavailable) else "Проверка памяти не завершилась")
        raise SystemExit(1)
