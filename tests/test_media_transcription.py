"""Private storage, cancellation, native routing and generic caption parsing."""
import json
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from services import media_transcription as media
from services.transcription_worker import parse_captions, public_metadata


class MediaTests(unittest.TestCase):
    def test_url_names_are_safe_distinct_and_stay_under_archive_directory(self):
        root = Path("archive")
        urls = ("https://public.test/video/1?x=1&y=2", "https://public.test/video/1?x=1_y=2")
        paths = [media.archive_path(url, root) for url in urls]
        self.assertNotEqual(*paths)
        for path in paths:
            self.assertEqual(path.parent, root)
            self.assertIn("public.test", path.name)
            self.assertFalse(any(char in path.name for char in '<>:"/\\|?*'))
        for url in ("file:///secret", "https://user:secret@public.test/video", "../unsafe"):
            with self.assertRaises(ValueError):
                media.archive_path(url, root)

    def test_archive_preserves_full_text_metadata_and_all_runs_and_cache_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("guest_transport.private_settings", return_value={}), \
             patch("guest_transport.configured_proxy", return_value="http://user:secret@proxy.test:80"), \
             patch.object(media, "run_job") as worker:
            url = "https://public.test/video"
            result = {"status": "read", "text_content": "я" * 51000, "metadata": {"title": "Title"},
                      "segments": [{"start": 0, "text": "word"}], "raw_subtitles": "raw caption"}
            worker.return_value = result
            first = media.transcribe_media(url, threading.Event(), directory=directory)
            second = media.transcribe_media(url, threading.Event(), directory=directory)
            self.assertEqual(worker.call_count, 1)
            self.assertEqual(second["cache"], "hit")
            self.assertLessEqual(len(second["text_content"]), 50000)
            self.assertLessEqual(len(json.dumps(second, ensure_ascii=False).encode("utf-8")), 101000)
            self.assertNotIn("raw_subtitles", second)
            self.assertEqual(worker.call_args.args[0]["model"], "large-v3-turbo")
            saved = json.loads(Path(first["saved_path"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["url"], url)
            self.assertEqual(saved["runs"][0]["result"], result)
            self.assertNotIn("secret", json.dumps(saved))
            worker.return_value = {"status": "read", "text_content": "new text"}
            media.transcribe_media(url, threading.Event(), directory=directory, refresh=True)
            self.assertEqual(len(json.loads(Path(first["saved_path"]).read_text(encoding="utf-8"))["runs"]), 2)

    def test_fast_quality_and_vocabulary_reach_worker(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("guest_transport.private_settings", return_value={}), \
             patch("guest_transport.configured_proxy", return_value=""), \
             patch.object(media, "run_job", return_value={"status": "unavailable", "reason": "no_speech"}) as worker:
            result = media.transcribe_media("https://public.test/video", threading.Event(), quality="fast",
                                           language="ru", vocabulary="ProductName", directory=directory)
            payload = worker.call_args.args[0]
            self.assertEqual((payload["model"], payload["language"], payload["vocabulary"]), ("tiny", "ru", "ProductName"))
            self.assertTrue(Path(result["saved_path"]).is_file(), "Failures must also be archived")

    def test_cancelled_request_never_runs_or_writes(self):
        cancel = threading.Event()
        cancel.set()
        with tempfile.TemporaryDirectory() as directory, \
             patch("guest_transport.private_settings", return_value={}), \
             patch.object(media, "run_job") as worker:
            result = media.transcribe_media("https://public.test/video", cancel, directory=directory)
            self.assertEqual(result["reason"], "cancelled")
            worker.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_archive_failure_does_not_claim_a_file_was_saved(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("guest_transport.private_settings", return_value={}), \
             patch("guest_transport.configured_proxy", return_value=""), \
             patch.object(media, "run_job", return_value={"status": "read", "text_content": "speech"}), \
             patch.object(media, "save_result", side_effect=PermissionError()):
            result = media.transcribe_media("https://public.test/video", threading.Event(), directory=directory)
        self.assertFalse(result["saved"])
        self.assertNotIn("saved_path", result)
        self.assertEqual(result["archive_error"], "archive_write_failed")

    def test_cancel_during_worker_never_saves_completed_result(self):
        cancel = threading.Event()
        def interrupted(*args, **kwargs):
            cancel.set()
            return {"status": "read", "text_content": "late result"}
        with tempfile.TemporaryDirectory() as directory, \
             patch("guest_transport.private_settings", return_value={}), \
             patch("guest_transport.configured_proxy", return_value=""), \
             patch.object(media, "run_job", side_effect=interrupted):
            result = media.transcribe_media("https://public.test/video", cancel, directory=directory)
            self.assertEqual(result["reason"], "cancelled")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_metadata_omits_network_secrets(self):
        result = public_metadata({"title": "Clip", "duration": 30, "url": "signed-secret", "http_headers": {"Cookie": "secret"},
                                  "formats": [{"url": "token"}], "cookies": "secret"})
        self.assertEqual(result, {"title": "Clip", "duration": 30})

    def test_extractor_redirect_to_private_network_is_rejected(self):
        import requests
        from yt_dlp import YoutubeDL
        from yt_dlp.networking.exceptions import RequestError
        from services.transcription_worker import run
        from test_guest_transport import response
        def extract(downloader, url, download=False):
            return downloader.urlopen(url)
        safe = lambda url: url == "https://public.test/video"
        payload = {"url": "https://public.test/video", "proxy": "http://user:secret@proxy.test:80", "timeout": 10}
        with patch("net_guard.is_safe_target", side_effect=safe), \
             patch("guest_transport.is_safe_target", side_effect=safe), \
             patch.object(YoutubeDL, "extract_info", extract), \
             patch.object(requests.Session, "request", return_value=response(302, headers={"Location": "http://127.0.0.1/private"})) as request:
            with self.assertRaises(RequestError):
                run(payload)
        self.assertEqual(request.call_count, 1)

    def test_vtt_and_json_captions_keep_times_and_text(self):
        text = "WEBVTT\n\n00:00:01.000 --> 00:00:02.500\nHello &amp; <b>world</b>\n"
        self.assertEqual(parse_captions(text, "vtt"), [{"start": 1, "end": 2.5, "text": "Hello & world"}])
        raw = json.dumps({"events": [{"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "Hello"}]}]})
        self.assertEqual(parse_captions(raw, "json3"), [{"start": 1, "end": 3, "text": "Hello"}])
        self.assertEqual(parse_captions("1\n00:00:01,000 --> 00:00:02,000\n12345\n\n", "srt")[0]["text"], "12345")

    def test_worker_cancellation_terminates_the_owned_process(self):
        original = subprocess.Popen
        def launch(*args, **kwargs):
            return original([sys.executable, "-I", "-c", "import sys,time; sys.stdin.buffer.read(); time.sleep(30)"], **kwargs)
        cancel = threading.Event()
        timer = threading.Timer(.4, cancel.set)
        started = time.monotonic()
        with patch.object(media.subprocess, "Popen", side_effect=launch):
            timer.start()
            try:
                result = media.run_job({}, cancel, timeout=10)
            finally:
                timer.cancel()
        self.assertEqual(result["reason"], "cancelled")
        self.assertLess(time.monotonic()-started, 6)

    def test_native_tool_calls_service_with_current_cancel_event(self):
        from native_codex.tools import execute
        cancel = threading.Event()
        with patch.object(media, "transcribe_media", return_value={"status": "read", "text_content": "speech"}) as service:
            self.assertEqual(execute("transcribe_media", {"url": "https://public.test/video"}, cancel, None)["status"], "read")
        self.assertIs(service.call_args.args[1], cancel)

    def test_native_schema_exposes_transcription_with_either_search_provider(self):
        from native_codex.schema import tools
        for provider in ("kid", "codex"):
            self.assertIn("transcribe_media", {item["name"] for item in tools(provider)})
