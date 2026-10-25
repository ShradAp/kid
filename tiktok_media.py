"""Bounded TikTok subtitles/audio jobs. No frames, images or author crawling."""
import copy
import os
from pathlib import Path
import subprocess
import threading
import time
import tempfile

from guest_transport import configured_proxy
from isolated_jobs import run_worker

_CACHE, _LOCK = {}, threading.Lock()
_AUDIO_SLOT = threading.BoundedSemaphore(1)


def read_tiktok_media(url, *, timeout=25, allow_audio=False, query=""):
    key = (url, configured_proxy(), allow_audio, query)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > time.monotonic():
            return {**copy.deepcopy(hit[1]), "media_cache": "hit"}
    audio_slot = allow_audio and _AUDIO_SLOT.acquire(blocking=False)
    payload = {"url": url, "proxy": key[1], "allow_audio": bool(audio_slot), "query": query,
               "timeout": max(.1, timeout), "deadline": time.monotonic()+timeout-.3,
               "model": os.environ.get("KID_SEARCH_WHISPER_MODEL", "tiny")}
    try:
        with tempfile.TemporaryDirectory(prefix="kid-media-job-") as folder:
            payload["work_dir"] = folder
            result = run_worker(Path(__file__).with_name("media_worker.py"), payload, timeout)
        # Never surface third-party stderr/tracebacks containing signed URLs or proxy credentials.
        if not result:
            result = {"media_status": "worker_failed"}
        if not isinstance(result, dict):
            result = {"media_status": "invalid_worker_response"}
    except subprocess.TimeoutExpired:
        result = {"media_status": "deadline"}
    except (OSError, ValueError):
        result = {"media_status": "worker_failed"}
    finally:
        if audio_slot:
            _AUDIO_SLOT.release()
    with _LOCK:
        if len(_CACHE) >= 64:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = (time.monotonic() + (3600 if result.get("text_content") else 60), copy.deepcopy(result))
    return result
