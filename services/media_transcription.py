"""Generic media transcription jobs and private, URL-named transcript archives."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

_SLOT = threading.BoundedSemaphore(1)
_STORAGE_LOCK = threading.RLock()


def archive_path(url, directory):
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
        raise ValueError("invalid_public_url")
    label = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", url).rstrip(" .")[:120]
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return Path(directory) / f"{label}--{digest}.json"


def save_result(path, url, options, result):
    with _STORAGE_LOCK:
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("url") != url or not isinstance(saved.get("runs"), list):
                saved = {}
        except (OSError, ValueError, AttributeError):
            saved = {}
        stamp = datetime.now(timezone.utc).isoformat()
        runs = saved.get("runs", [])
        runs.append({"created_at": stamp, "options": options, "result": result})
        document = {"schema_version": 1, "url": url, "updated_at": stamp, "runs": runs}
        path.parent.mkdir(parents=True, exist_ok=True)
        # A unique temporary avoids partially written archives and collisions.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, ensure_ascii=False, indent=2)
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def cached_result(path, url, options):
    with _STORAGE_LOCK:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if document.get("url") != url:
                return None
            for run in reversed(document.get("runs", [])):
                result = run.get("result", {})
                if (run.get("options") == options and result.get("status") == "read"
                        and not result.get("transcript_truncated")):
                    return result
        except (OSError, ValueError, AttributeError):
            pass
    return None


def model_result(result, path, cache):
    public = dict(result)
    public.pop("raw_subtitles", None)
    text = public.get("text_content", "")
    if len(text) > 50000:
        public["text_content"] = text[:50000]
        public["response_truncated"] = True
    if len(public.get("segments", [])) > 1000:
        public["segments"] = public["segments"][:1000]
        public["response_truncated"] = True
    # Word confidences remain in the archive; the model needs segment timings.
    if "segments" in public:
        public["segments"] = [{k: v for k, v in segment.items() if k in ("start", "end", "text")}
                              for segment in public["segments"]]
    if len(json.dumps(public, ensure_ascii=False).encode("utf-8")) > 100000:
        public["segments"] = []
        public["text_content"] = public.get("text_content", "")[:25000]
        public["metadata"] = {k: v for k, v in public.get("metadata", {}).items() if k in ("title", "duration", "language")}
        public["response_truncated"] = True
    return {**public, "saved_path": str(path), "cache": cache, "saved": True}


def run_job(payload, cancel, timeout=240):
    """Only the owned worker/process tree is terminated on cancel/deadline."""
    job = None
    process = None
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile() as output:
        try:
            if os.name == "nt":
                from native_codex.job import ProcessJob
                job = ProcessJob()
            script = Path(__file__).with_name("transcription_worker.py")
            process = subprocess.Popen([sys.executable, "-I", str(script)], stdin=subprocess.PIPE,
                stdout=output, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt")
            if job:
                job.assign(process)
            process.stdin.write(json.dumps(payload).encode("utf-8"))
            process.stdin.close()
            while process.poll() is None:
                if cancel.wait(.1):
                    return {"status": "unavailable", "reason": "cancelled"}
                if time.monotonic() >= deadline:
                    return {"status": "unavailable", "reason": "transcription_deadline"}
                if os.fstat(output.fileno()).st_size > 8_000_000:
                    return {"status": "unavailable", "reason": "result_size_limit"}
            if process.returncode:
                return {"status": "unavailable", "reason": "worker_failed"}
            output.seek(0)
            raw = output.read(8_000_001)
            if len(raw) > 8_000_000:
                return {"status": "unavailable", "reason": "result_size_limit"}
            result = json.loads(raw)
            return result if isinstance(result, dict) else {"status": "unavailable", "reason": "invalid_worker_result"}
        finally:
            if process is not None:
                if job:
                    job.terminate()
                elif process.poll() is None:
                    import signal
                    os.killpg(process.pid, signal.SIGKILL)
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
            if job:
                job.close()


def transcribe_media(url, cancel, *, quality="accurate", language=None, vocabulary="", refresh=False,
                     directory=None):
    from guest_transport import configured_proxy, private_settings
    from app_paths import USER_DATA_DIR
    if not isinstance(url, str) or len(url) > 4096 or quality not in ("accurate", "fast"):
        raise ValueError("invalid_transcription_options")
    if language is not None and (not isinstance(language, str) or not re.fullmatch(r"[a-z]{2,3}", language)):
        raise ValueError("invalid_language")
    if not isinstance(vocabulary, str) or len(vocabulary) > 1000 or not isinstance(refresh, bool):
        raise ValueError("invalid_transcription_options")
    path = archive_path(url, Path(directory) if directory is not None else USER_DATA_DIR / "transcriptions")
    settings = private_settings()
    model = settings.get("media_transcription_model", "large-v3-turbo") if quality == "accurate" else "tiny"
    if not isinstance(model, str) or not model.strip() or len(model) > 512:
        raise ValueError("invalid_speech_model")
    options = {"quality": quality, "model": model, "language": language, "vocabulary": vocabulary}
    if cancel.is_set():
        return {"status": "unavailable", "reason": "cancelled"}
    cached = None if refresh else cached_result(path, url, options)
    if cached:
        return model_result(cached, path, "hit")
    if cancel.is_set():
        return {"status": "unavailable", "reason": "cancelled"}
    # Avoid concurrent large models exhausting host RAM; waiting is cancellable.
    deadline = time.monotonic() + 250
    while not _SLOT.acquire(timeout=.1):
        if cancel.is_set():
            return {"status": "unavailable", "reason": "cancelled"}
        if time.monotonic() >= deadline:
            return {"status": "unavailable", "reason": "transcription_busy"}
    try:
        if cancel.is_set():
            return {"status": "unavailable", "reason": "cancelled"}
        cached = None if refresh else cached_result(path, url, options)
        if cached:
            return model_result(cached, path, "hit")
        timeout = min(240, max(1, deadline-time.monotonic()))
        payload = {"url": url, "proxy": configured_proxy(), **options, "max_duration": 600,
                   "max_bytes": 64_000_000, "timeout": max(.1, timeout-10)}
        try:
            result = run_job(payload, cancel, timeout=timeout)
        except (OSError, ValueError):
            result = {"status": "unavailable", "reason": "worker_failed"}
        if cancel.is_set() or result.get("reason") == "cancelled":
            return {"status": "unavailable", "reason": "cancelled"}
        try:
            save_result(path, url, options, result)
        except OSError:
            public = model_result(result, path, "miss")
            public.pop("saved_path", None)
            return {**public, "saved": False, "archive_error": "archive_write_failed"}
        return {**model_result(result, path, "miss"), "saved": True}
    finally:
        _SLOT.release()
