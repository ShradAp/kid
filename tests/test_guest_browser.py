"""Browser fallback tests use local fixtures, never accounts or public sites."""
import http.server
import gzip
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import guest_browser as browser


class ProxyTests(unittest.TestCase):
    def test_login_button_inside_article_does_not_mark_article_unavailable(self):
        self.assertEqual(browser.unreadable_page("Log in\nThe full article explains how the application works."), "")
        self.assertEqual(browser.unreadable_page("Log in"), "login_screen")

    def test_media_requested_as_fetch_is_blocked_without_site_rules(self):
        for url, headers in (("https://public.test/clip.mp4?token=abc", {}),
                             ("https://public.test/opaque-resource", {"range": "bytes=0-65535"})):
            self.assertTrue(browser.non_text_resource(SimpleNamespace(resource_type="fetch", url=url, headers=headers)))
        self.assertFalse(browser.non_text_resource(SimpleNamespace(resource_type="fetch", url="https://public.test/article.json", headers={})))

    def test_proxy_auth_is_passed_separately_from_server(self):
        options = browser.proxy_options("http://user%40name:p%3Ass@proxy.test:8080")
        self.assertEqual(options["server"], "http://proxy.test:8080")
        self.assertEqual(options["username"], "user@name")
        self.assertEqual(options["password"], "p:ss")
        self.assertEqual(options["bypass"], "<-loopback>")

    def test_authenticated_socks_is_explicitly_rejected_without_direct_fallback(self):
        result = browser.read_page("https://public.test/", threading.Event(),
                                   proxy_url="socks5h://user:secret@proxy.test:1080")
        self.assertEqual(result["reason"], "browser_proxy_unsupported")
        self.assertEqual(result["route"], "proxy")
        self.assertNotIn("secret", str(result))

    def test_cancelled_read_never_starts_browser(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(browser, "_read") as read:
            self.assertEqual(browser.read_page("https://public.test/", cancel, proxy_url="")["reason"], "cancelled")
        read.assert_not_called()


class Fixture(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/login", "/player-error"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b'<html><body>Log in</body></html>' if self.path == "/login"
                             else b'<html><body>Playback error</body></html>')
            return
        if self.path == "/gzip.js":
            body = gzip.compress(b'document.body.textContent="Compressed script executed";')
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/gzip-app":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b'<html><body><script src="/gzip.js"></script></body></html>')
            return
        if self.path in ("/redirect", "/unsafe"):
            self.send_response(302)
            self.send_header("Location", "/app" if self.path == "/redirect" else "http://127.0.0.1:9/private")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = (b'<html><title>Fixture</title><body><script>setTimeout(()=>document.body.textContent="Rendered by JavaScript",100)</script></body></html>'
                if self.path == "/app" else b'<html><body>Please wait...<script>setInterval(()=>{},1000)</script></body></html>')
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).is_file():
                raise unittest.SkipTest("Install Chromium with playwright install chromium")
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(2)

    def render(self, path, timeout=8):
        # Permit ONLY this test server; other local/private addresses stay blocked.
        with patch.object(browser, "is_safe_target", side_effect=lambda url: url.startswith(self.url + "/")):
            return browser.read_page(self.url + path, threading.Event(), proxy_url="", timeout=timeout)

    def test_executes_javascript_and_follows_validated_redirect(self):
        result = self.render("/redirect")
        self.assertEqual(result["status"], "read", result)
        self.assertIn("Rendered by JavaScript", result["text_content"])
        self.assertEqual(result["read_method"], "browser")

    def test_compressed_javascript_is_executed(self):
        result = self.render("/gzip-app", timeout=4)
        self.assertEqual(result["status"], "read", result)
        self.assertIn("Compressed script executed", result["text_content"])

    def test_login_screen_and_player_error_are_not_readable_content(self):
        for path, reason in (("/login", "login_screen"), ("/player-error", "playback_unavailable")):
            with self.subTest(path=path):
                result = self.render(path, timeout=4)
                self.assertEqual(result["status"], "unavailable", result)
                self.assertEqual(result["reason"], reason)

    def test_redirect_to_unapproved_private_address_is_blocked(self):
        result = self.render("/unsafe")
        self.assertEqual(result["status"], "unavailable", result)
        self.assertEqual(result["reason"], "unsafe_target")

    def test_challenge_never_becomes_successful_text(self):
        result = self.render("/challenge", timeout=4)
        self.assertEqual(result["status"], "unavailable", result)
        self.assertIn(result["reason"], ("deadline", "browser_challenge"))

    def test_cancellation_stops_an_active_browser_read(self):
        cancel = threading.Event()
        timer = threading.Timer(1.5, cancel.set)
        with patch.object(browser, "is_safe_target", side_effect=lambda url: url.startswith(self.url + "/")):
            timer.start()
            try:
                result = browser.read_page(self.url + "/challenge", cancel, proxy_url="", timeout=8)
            finally:
                timer.cancel()
        self.assertEqual(result["reason"], "cancelled")
