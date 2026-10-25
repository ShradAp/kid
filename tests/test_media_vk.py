"""Bounded speech extraction, exact VK post identity and search refinements."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

import media_worker as m
import public_sources as p
import search_tools
import tiktok_media as t
import vk_public as v
from isolated_jobs import run_worker
from research_search import SearchBudget, investigate


class MediaTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(t._CACHE, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_tiktok_music_is_not_the_video_speech_track(self):
        music = {"format_id": "audio", "url": "https://cdn.test/song", "vcodec": "none", "acodec": "mp3"}
        video = {"format_id": "video", "url": "https://cdn.test/video", "vcodec": "h264", "acodec": "aac", "filesize": 1000}
        selected, _ = m.select_audio({"duration": 131, "formats": [music, video]})
        self.assertEqual(selected, video)

    def test_long_unknown_and_large_media_are_skipped(self):
        for duration in (None, 0, 1801):
            self.assertIsNone(m.select_audio({"duration": duration, "formats": []})[0])
        video = {"url": "https://cdn.test/video", "vcodec": "h264", "acodec": "aac", "filesize": 9_000_000}
        self.assertIsNone(m.select_audio({"duration": 30, "formats": [video]})[0])

    def test_subtitles_keep_timecodes(self):
        text = m.parse_subtitles("WEBVTT\n\n00:00:03.000 --> 00:00:05.000\n<b>needle</b> answer\n", "vtt")
        self.assertEqual(text, "[00:00:03] needle answer")
        text = m.parse_subtitles(json.dumps({"utterances": [{"start_time": 65000, "text": "answer"}]}), "json")
        self.assertIn("[00:01:05]", text)

    def test_audio_decoder_never_requests_video_frames_and_caps_samples(self):
        import av
        import numpy as np
        container = MagicMock()
        container.__enter__.return_value = container
        container.decode.return_value = iter([object()] * 100)
        audio = Mock()
        audio.to_ndarray.return_value = np.ones((1, 32000), dtype=np.int16)
        resampler = Mock()
        resampler.resample.return_value = [audio]
        with patch.object(av, "open", return_value=container), patch.object(av, "AudioResampler", return_value=resampler):
            waveform = m.decode_audio_bounded("unused", time.monotonic()+5, seconds=1)
        container.decode.assert_called_once_with(audio=0)
        self.assertEqual(len(waveform), 16000)
        self.assertEqual(waveform.dtype, np.float32)

    def test_profiles_are_never_crawled(self):
        self.assertEqual(m.run({"url": "https://www.tiktok.com/@author", "timeout": 1})["media_status"], "single_video_url_required")

    def test_parent_owns_temp_files_and_caches_result(self):
        folders = []
        def work(script, payload, timeout):
            folders.append(payload["work_dir"])
            self.assertTrue(Path(payload["work_dir"]).is_dir())
            (Path(payload["work_dir"]) / "audio.media").write_bytes(b"scratch")
            return {"text_content": "needle", "content_kind": "speech_transcript"}
        with patch.object(t, "configured_proxy", return_value="http://secret:private@proxy.test"), \
             patch.object(t, "run_worker", side_effect=work) as worker:
            result = t.read_tiktok_media("https://www.tiktok.com/@author/video/123", query="needle")
            second = t.read_tiktok_media("https://www.tiktok.com/@author/video/123", query="needle")
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(second["media_cache"], "hit")
        self.assertFalse(Path(folders[0]).exists())
        self.assertNotIn("secret", json.dumps(result))

    def test_worker_timeout_is_returned_without_stderr_or_secret(self):
        with patch.object(t, "configured_proxy", return_value=""), \
             patch.object(t, "run_worker", side_effect=subprocess.TimeoutExpired("worker", .1)):
            result = t.read_tiktok_media("https://www.tiktok.com/@author/video/123", timeout=.1)
        self.assertEqual(result["media_status"], "deadline")

    def test_real_local_worker_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "sleep.py"
            script.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
            start = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                run_worker(script, {}, .2)
            self.assertLess(time.monotonic()-start, 4)

    def test_one_audio_job_per_search(self):
        batch = {"status": "ok", "items": [{"url": f"https://www.tiktok.com/@test/video/{i}", "title": "needle"} for i in range(3)]}
        with patch.object(search_tools, "_searxng_search", return_value=batch), \
             patch.object(p, "read_public", return_value={"text_content": "desc", "content_kind": "video_description"}) as read:
            search_tools.web_search("needle", backend="searxng", budget=SearchBudget(max_requests=1, max_reads=3, max_transcripts=3, max_audio_jobs=1))
        self.assertEqual([c.kwargs["allow_audio"] for c in read.call_args_list], [True, False, False])


class VKTests(unittest.TestCase):
    def test_related_posts_cannot_be_used_as_requested_post(self):
        value = {"items": [{"owner_id": -2, "id": 3, "text": "wrong owner"},
                           {"owner_id": -1, "id": 4, "text": "wrong post"},
                           {"owner_id": -1, "id": 3, "text": "exact"}]}
        self.assertEqual(v.exact_post(value, "-1", "3"), "exact")
        self.assertEqual(v.exact_post(value, "-1", "5"), "")

    def test_service_api_uses_configured_key_but_does_not_return_it(self):
        session = MagicMock()
        session.__enter__.return_value = session
        session.post.return_value.json.return_value = {"response": [{"owner_id": -1, "id": 2, "text": "exact"}]}
        with patch.object(v, "private_settings", return_value={"vk_service_token": "private-key"}), \
             patch.object(v, "GuestSession", return_value=session), patch.object(v, "run_worker") as browser:
            result = v.read_vk_fallback("https://vk.ru/wall-1_2")
        browser.assert_not_called()
        self.assertEqual(result["text_content"], "exact")
        self.assertEqual(session.post.call_args.kwargs["data"]["access_token"], "private-key")
        self.assertNotIn("private-key", json.dumps(result))

    def test_missing_key_and_disabled_browser_are_explicit(self):
        with patch.object(v, "private_settings", return_value={}), patch.dict("os.environ", {"KID_VK_SERVICE_TOKEN": ""}), \
             patch.object(v, "run_worker") as browser:
            result = v.read_vk_fallback("https://vk.ru/wall-1_2", allow_browser=False)
        browser.assert_not_called()
        self.assertEqual(result["vk_methods"], {"service_api": "not_configured", "guest_browser": "budget_skipped"})

    def test_browser_worker_gets_exact_post_identity(self):
        with patch.object(v, "private_settings", return_value={}), patch.dict("os.environ", {"KID_VK_SERVICE_TOKEN": ""}), \
             patch.object(v, "configured_proxy", return_value=""), \
             patch.object(v, "run_worker", return_value={"status": "read_dom", "text": "exact"}) as worker:
            result = v.read_vk_fallback("https://vk.ru/wall-1_2")
        self.assertEqual(worker.call_args.args[1]["owner"], "-1")
        self.assertEqual(worker.call_args.args[1]["post"], "2")
        self.assertEqual(result["vk_methods"]["guest_browser"], "read_dom")

    def test_profile_url_is_not_a_post(self):
        with patch.object(v, "run_worker") as worker:
            result = v.read_vk_fallback("https://vk.ru/example")
        worker.assert_not_called()
        self.assertEqual(result["vk_methods"]["guest_browser"], "exact_post_required")


class SourceTests(unittest.TestCase):
    def test_fallbacks_do_not_displace_unvisited_source_directions(self):
        calls = []
        def search(provider, query, *args):
            calls.append(provider)
            return {"status": "empty", "items": [], "retry_provider": "fallback:"+provider}
        providers = ("base", "base:forums", "base:reviews", "base:articles", "base:technical", "base:apps")
        investigate("needle", search=search, read=Mock(), providers=providers,
                    budget=SearchBudget(max_requests=6, max_reads=0, max_rounds=1))
        self.assertEqual(calls, list(providers))

    def test_new_directions_match_real_domains_only(self):
        self.assertEqual(p.scope_for("https://github.com/org/repo/issues/1"), "technical")
        self.assertEqual(p.scope_for("https://4pda.to/forum/"), "forums")
        self.assertEqual(p.scope_for("https://otzovik.com/review_1.html"), "reviews")
        self.assertEqual(p.scope_for("https://habr.com/ru/articles/1"), "articles")
        self.assertEqual(p.scope_for("https://github.com.attacker.test"), "")

    def test_refinement_is_not_starved_by_scope_pagination(self):
        calls = []
        def search(provider, query, *args):
            calls.append(query)
            return {"status": "ok", "items": [{"url": "https://example.com/" + str(len(calls)), "title": 'needle «Hidden Model»'}]}
        result = investigate("needle", search=search, read=Mock(), providers=("base", "base:reviews", "base:forums"),
                             budget=SearchBudget(max_requests=4, max_reads=0, max_rounds=2))
        self.assertIn("Hidden Model", calls[3])
        self.assertEqual(result.report["requests"], 4)


if __name__ == "__main__":
    unittest.main()
