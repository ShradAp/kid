"""Guest adapters: format drift, exact source identity, access and evidence labels."""
import json
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import public_sources as p
import guest_transport as gt
import search_tools
from research_search import SearchBudget, investigate, _terms


class PublicSourceTests(unittest.TestCase):
    def setUp(self):
        # Fixture page tests must never invoke the live library/network fallback.
        patcher = patch.object(p, "fetch_captions", return_value={"caption_status": "fixture_unavailable"})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("read_tiktok_media", {"media_status": "fixture_unavailable"}),
                            ("read_vk_fallback", {"vk_methods": {"guest_browser": "fixture_unavailable"}})):
            patcher = patch.object(p, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
    def test_explicit_fetch_webpage_also_uses_guest_reader(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "network_agent" / "core.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NetworkAgentCore")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "fetch_webpage")
        namespace = {}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "core-fetch-method", "exec"), namespace)
        core = SimpleNamespace(_get_page_data=Mock())
        with patch.object(p, "read_public", return_value={"url": "https://youtu.be/abcdefghijk", "title": "needle",
                                                         "text_content": "description", "content_kind": "video_description"}):
            result = namespace["fetch_webpage"](core, "https://youtu.be/abcdefghijk")
        core._get_page_data.assert_not_called()
        self.assertIn("полный материал не прочитан", result)

    def test_host_matching_is_exact(self):
        self.assertEqual(p.platform_for("https://m.youtube.com/watch?v=abc"), "youtube")
        self.assertEqual(p.platform_for("https://vk.ru/wall-1_2"), "vk")
        self.assertEqual(p.platform_for("https://youtube.com.attacker.test/"), "")
        self.assertEqual(p.platform_for("https://attacker.test/youtube.com"), "")

    def test_scoped_query_and_aggregator_exclusion(self):
        self.assertIn("site:vk.ru", p.scoped_query("needle", "vk"))
        self.assertFalse(p.useful_candidate("https://www.tiktok.com/discover/noise"))
        self.assertTrue(p.useful_candidate("https://www.tiktok.com/@author/video/123"))

    def test_named_entity_transliteration(self):
        self.assertIn("kakadu", _terms("какаду"))
        self.assertIn("ai", _terms("Kakadu AI"))

    def test_youtube_native_search_and_no_fake_pagination(self):
        data = {"contents": [{"videoRenderer": {"videoId": "abcdefghijk", "title": {"runs": [{"text": "needle"}]},
                                                "ownerText": {"simpleText": "Author"}}}]}
        with patch.object(p, "public_get", return_value="var ytInitialData = " + json.dumps(data) + ";") as get:
            result = p.youtube_search("needle")
            second = p.youtube_search("needle", page=2)
        self.assertEqual(result["items"][0]["author"], "Author")
        self.assertFalse(result["has_more"])
        self.assertEqual(second["status"], "unsupported")
        self.assertEqual(get.call_count, 1)

    def test_youtube_login_page_is_not_empty_search(self):
        with patch.object(p, "public_get", return_value="<h1>Sign in</h1>"):
            self.assertEqual(p.youtube_search("needle")["status"], "unavailable")

    def test_youtube_description_is_not_transcript(self):
        data = {"videoDetails": {"title": "Title", "shortDescription": "needle description", "author": "A"}}
        with patch.object(p, "public_get", return_value="var ytInitialPlayerResponse = " + json.dumps(data)):
            result = p.read_public("https://youtu.be/abcdefghijk")
        self.assertEqual(result["content_kind"], "video_description")
        self.assertEqual(result["text_content"], "needle description")

    def test_public_captions_keep_timestamps(self):
        player = {"videoDetails": {"title": "Title"}, "captions": {"playerCaptionsTracklistRenderer": {
            "captionTracks": [{"baseUrl": "https://www.youtube.com/api/timedtext?v=abcdefghijk", "languageCode": "ru", "kind": "asr"}]}}}
        with patch.object(p, "public_get", side_effect=["ytInitialPlayerResponse=" + json.dumps(player),
                                                      '<transcript><text start="65.4">needle &amp; answer</text></transcript>']):
            result = p.read_public("https://youtube.com/watch?v=abcdefghijk")
        self.assertEqual(result["content_kind"], "transcript")
        self.assertIn("[00:01:05] needle & answer", result["text_content"])
        self.assertTrue(result["automatic_captions"])

    def test_caption_url_cannot_point_to_private_service(self):
        player = {"videoDetails": {"title": "T"}, "captions": {"playerCaptionsTracklistRenderer": {
            "captionTracks": [{"baseUrl": "http://127.0.0.1/private"}]}}}
        with patch.object(p, "public_get", return_value="ytInitialPlayerResponse=" + json.dumps(player)) as get:
            result = p.read_public("https://youtu.be/abcdefghijk")
        self.assertEqual(get.call_count, 1)
        self.assertEqual(result["content_kind"], "video_description")

    def test_telegram_exact_post_excludes_navigation_and_scripts(self):
        text = '<div>Sign in</div><div class="tgme_widget_message_text">needle <b>answer</b><br>line<script>bad</script></div>'
        with patch.object(p, "public_get", return_value=text) as get:
            result = p.read_public("https://t.me/s/channel/123")
        self.assertEqual(get.call_args.args[0], "https://t.me/channel/123?embed=1")
        self.assertEqual(result["content_kind"], "post")
        self.assertEqual(result["text_content"], "needle answer\nline")

    def test_telegram_channel_feed_is_not_an_exact_post(self):
        with patch.object(p, "public_get") as get:
            result = p.read_public("https://t.me/s/channel?before=100")
        get.assert_not_called()
        self.assertEqual(result["error"], "public_message_url_required")

    def test_vk_only_reads_requested_post(self):
        text = '<div id="post-1_2"><div class="wall_post_text">needle exact</div></div>'
        text += '<div id="post-1_3"><div class="wall_post_text">unrelated recommendation</div></div>'
        with patch.object(p, "public_get", return_value=text):
            result = p.read_public("https://vk.ru/wall-1_2")
        self.assertEqual(result["text_content"], "needle exact")
        self.assertEqual(result["content_kind"], "post")

    def test_vk_login_is_not_post_content(self):
        with patch.object(p, "public_get", return_value='<meta property="og:description" content="Войдите в аккаунт">'):
            result = p.read_public("https://vk.com/wall-1_2")
        self.assertEqual(result["content_kind"], "unavailable")

    def test_tiktok_uses_matching_video_only(self):
        state = {"items": [{"id": "456", "desc": "unrelated"}, {"id": "123", "desc": "needle description"}]}
        with patch.object(p, "public_get", return_value='<script id="SIGI_STATE">' + json.dumps(state) + '</script>'):
            result = p.read_public("https://www.tiktok.com/@author/video/123")
        self.assertEqual(result["text_content"], "needle description")
        self.assertEqual(result["content_kind"], "video_description")

    def test_tiktok_oembed_fallback_is_description_only(self):
        with patch.object(p, "public_get", side_effect=["<html>challenge</html>", json.dumps({"title": "needle", "author_name": "A"})]):
            result = p.read_public("https://www.tiktok.com/@author/video/123")
        self.assertEqual(result["content_kind"], "video_description")
        self.assertEqual(result["author"], "A")

    def test_no_fallback_to_browser_for_blocked_social_page(self):
        batch = {"status": "ok", "items": [{"url": "https://vk.ru/wall-1_2", "body": "needle", "title": "needle"}]}
        with patch.object(search_tools, "_searxng_search", return_value=batch), \
             patch.object(p, "read_public", return_value={"error": "blocked"}), \
             patch.object(search_tools, "investigate", wraps=investigate):
            browser = Mock()
            results, error = search_tools.web_search("needle", backend="searxng", reader=browser,
                                                     budget=SearchBudget(max_requests=1, max_reads=1))
        browser.assert_not_called()
        self.assertEqual(results[0]["read_status"], "unavailable")

    def test_metadata_is_not_counted_as_read_video(self):
        results = investigate("needle", providers=("test:youtube",), budget=SearchBudget(max_requests=1),
                              search=lambda *a: {"status": "ok", "items": [{"url": "https://youtu.be/abcdefghijk", "title": "needle"}]},
                              read=lambda url: {"text_content": "needle description", "content_kind": "video_description"})
        self.assertEqual(results[0]["read_status"], "metadata_only")
        self.assertEqual(results.report["coverage"]["youtube"]["read"], 0)
        self.assertEqual(results.report["coverage"]["youtube"]["metadata"], 1)

    def test_index_fallback_is_counted_as_separate_request(self):
        calls = []
        def search(provider, *args):
            calls.append(provider)
            return {"status": "empty", "items": [], **({"retry_provider": "serper:vk"} if provider == "searxng:vk" else {})}
        results = investigate("needle", providers=("searxng:vk",), search=search, read=Mock(), budget=SearchBudget(max_requests=2))
        self.assertEqual(calls, ["searxng:vk", "serper:vk"])
        self.assertEqual(results.report["requests"], 2)

    def test_redirects_are_revalidated_without_cookies(self):
        redirect = Mock(status_code=302, headers={"Location": "http://127.0.0.1/private"})
        with gt.GuestSession(proxy_url="") as session, \
             patch.object(gt.requests.Session, "request", return_value=redirect) as request, \
             patch.object(gt, "is_safe_target", side_effect=[True, False]):
            with self.assertRaisesRegex(gt.GuestTransportError, "unsafe_target"):
                p.public_get("https://t.me/channel/123", transport=session)
            self.assertFalse(session.trust_env)
            self.assertEqual(request.call_count, 1)
            self.assertFalse(request.call_args.kwargs["allow_redirects"])


if __name__ == "__main__":
    unittest.main()
