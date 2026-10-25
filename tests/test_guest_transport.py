"""Proxy routing, deadlines, resource caps and free caption extraction."""
import ast
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

import guest_transport as g
import public_sources
import search_tools
import youtube_captions as c
from research_search import SearchBudget


def response(code=200, body=b"ok", headers=None):
    result = requests.Response()
    result.status_code = code
    result.url = "https://t.me/channel/1"
    result.headers.update(headers or {})
    result._content = body
    result._content_consumed = True
    result.close = Mock()
    return result


class TransportTests(unittest.TestCase):
    def test_config_credentials_are_encoded_and_explicit_direct_is_respected(self):
        with tempfile.TemporaryDirectory() as root, patch.dict("os.environ", {"KID_CONFIG_DIR": root}):
            target = Path(root) / "config.json"
            target.write_text(json.dumps({"proxy_mode": "custom", "proxy_url": "127.0.0.1:1234",
                                          "proxy_user": "user@name", "proxy_pass": "p:a/ss"}), encoding="utf-8")
            self.assertEqual(g.configured_proxy(), "http://user%40name:p%3Aa%2Fss@127.0.0.1:1234")
            target.write_text(json.dumps({"proxy_mode": "direct", "proxy_url": "http://127.0.0.1:1234"}), encoding="utf-8")
            self.assertEqual(g.configured_proxy(), "")

    def test_proxy_first_and_direct_fallback_without_leaking_secret(self):
        with g.GuestSession(proxy_url="http://private-user:private-password@proxy.test:80") as session, \
             patch.object(g, "is_safe_target", return_value=True), \
             patch.object(requests.Session, "request", side_effect=[requests.exceptions.ProxyError("private-password"), response()]) as request:
            result = session.get("https://t.me/channel/1")
        self.assertEqual(result.text, "ok")
        self.assertIn("proxy.test", request.call_args_list[0].kwargs["proxies"]["https"])
        self.assertIsNone(request.call_args_list[1].kwargs["proxies"]["https"])
        self.assertNotIn("private-password", json.dumps(session.diagnostics()))
        self.assertNotIn("private-user", json.dumps(session.diagnostics()))

    def test_successful_proxy_does_not_retry_direct(self):
        with g.GuestSession(proxy_url="http://proxy.test:80") as session, \
             patch.object(g, "is_safe_target", return_value=True), \
             patch.object(requests.Session, "request", return_value=response()) as request:
            session.get("https://t.me/channel/1")
        self.assertEqual(request.call_count, 1)

    def test_request_cap_includes_redirects(self):
        with g.GuestSession(proxy_url="", max_requests=1) as session, \
             patch.object(g, "is_safe_target", return_value=True), \
             patch.object(requests.Session, "request", return_value=response(302, headers={"Location": "https://t.me/next"})) as request:
            with self.assertRaisesRegex(g.GuestTransportError, "request_limit"):
                session.get("https://t.me/channel/1")
        self.assertEqual(request.call_count, 1)

    def test_byte_cap(self):
        with g.GuestSession(proxy_url="", max_bytes=2) as session, \
             patch.object(g, "is_safe_target", return_value=True), \
             patch.object(requests.Session, "request", return_value=response(body=b"too large")):
            with self.assertRaisesRegex(g.GuestTransportError, "byte_limit"):
                session.get("https://t.me/channel/1")

    def test_expired_deadline_starts_no_request(self):
        with g.GuestSession(proxy_url="", timeout=0) as session, patch.object(requests.Session, "request") as request:
            with self.assertRaisesRegex(g.GuestTransportError, "deadline"):
                session.get("https://t.me/channel/1")
        request.assert_not_called()

    def test_caption_session_rejects_unexpected_hosts_and_account_headers(self):
        with g.GuestSession(proxy_url="", allowed_domains=("youtube.com",)) as session:
            with self.assertRaisesRegex(g.GuestTransportError, "unexpected_domain"):
                session.get("https://other.test/")
            with self.assertRaisesRegex(g.GuestTransportError, "account_credentials_forbidden"):
                session.get("https://youtube.com/", headers={"Authorization": "secret"})

    def test_cookie_jar_is_cleared(self):
        with g.GuestSession(proxy_url="") as session, patch.object(g, "is_safe_target", return_value=True), \
             patch.object(requests.Session, "request", return_value=response()):
            session.cookies.set("session", "account-cookie")
            session.get("https://t.me/channel/1")
            self.assertFalse(session.cookies)
            self.assertFalse(session.trust_env)

    def test_old_proxy_logger_redacts_userinfo(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "network_agent" / "proxy_env.py").read_text(encoding="utf-8"))
        method = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "redact_proxy_url")
        from urllib.parse import urlparse
        namespace = {"urlparse": urlparse}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "proxy-redaction", "exec"), namespace)
        self.assertEqual(namespace["redact_proxy_url"]("http://user:secret@proxy.test:80"), "http://proxy.test:80")


class CaptionTests(unittest.TestCase):
    def setUp(self):
        self.cache_patch = patch.dict(c._CACHE, {}, clear=True)
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)

    def track(self, snippets=None, language="ru"):
        return SimpleNamespace(language_code=language, is_generated=True,
            fetch=Mock(return_value=snippets or [SimpleNamespace(start=1800, text="needle at minute thirty")]))

    def test_long_video_captions_are_ready_text_with_timestamp_and_cache(self):
        from youtube_transcript_api import YouTubeTranscriptApi
        track = self.track()
        with patch.object(YouTubeTranscriptApi, "list", return_value=[track]) as listing, g.GuestSession(proxy_url="") as session:
            first = c.fetch_captions("abcdefghijk", session)
            second = c.fetch_captions("abcdefghijk", session)
        self.assertEqual(first["content_kind"], "transcript")
        self.assertIn("[00:30:00]", first["text_content"])
        self.assertEqual(second["caption_cache"], "hit")
        self.assertEqual(listing.call_count, 1)

    def test_caption_output_limits_are_explicit(self):
        from youtube_transcript_api import YouTubeTranscriptApi
        track = self.track([SimpleNamespace(start=i, text="x" * 40) for i in range(100)])
        with patch.object(YouTubeTranscriptApi, "list", return_value=[track]), g.GuestSession(proxy_url="") as session:
            result = c.fetch_captions("abcdefghijk", session, max_chars=120, max_segments=2)
        self.assertLessEqual(len(result["text_content"]), 120)
        self.assertTrue(result["transcript_truncated"])

    def test_caption_failure_is_cached_and_contains_no_exception_message(self):
        from youtube_transcript_api import YouTubeTranscriptApi
        with patch.object(YouTubeTranscriptApi, "list", side_effect=ValueError("private proxy secret")) as listing, \
             g.GuestSession(proxy_url="") as session:
            first = c.fetch_captions("abcdefghijk", session)
            c.fetch_captions("abcdefghijk", session)
        self.assertEqual(first["caption_status"], "ValueError")
        self.assertNotIn("secret", json.dumps(first))
        self.assertEqual(listing.call_count, 1)

    def test_only_two_video_transcript_attempts_per_search(self):
        batch = {"status": "ok", "items": [{"url": f"https://youtube.com/watch?v=abcdefghij{i}", "title": "needle"} for i in range(3)]}
        with patch.object(search_tools, "_searxng_search", return_value=batch), \
             patch.object(public_sources, "read_public", return_value={"text_content": "description", "content_kind": "video_description"}) as read:
            search_tools.web_search("needle", backend="searxng", budget=SearchBudget(max_requests=1, max_reads=3, max_transcripts=2))
        self.assertEqual([call.kwargs["fetch_transcript"] for call in read.call_args_list], [True, True, False])


if __name__ == "__main__":
    unittest.main()
