"""Регрессии доступа к реальному HTTP-мосту с изолированными данными."""
import http.client
import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.parse import quote

from bridge_security import FileAccess, retained_secret


class FileAccessTests(unittest.TestCase):
    def test_fresh_windows_profile_is_initialized_without_personal_files(self):
        with tempfile.TemporaryDirectory(prefix="kid-first-run-") as directory:
            env = dict(os.environ, APPDATA=directory, PYTHONDONTWRITEBYTECODE="1")
            env.pop("KID_CONFIG_DIR", None)
            code = '''
from pathlib import Path
import os, config_manager as cm
cfg = cm.load_config()
expected = Path(os.environ["APPDATA"]) / "kid"
assert cm.CONFIG_DIR == expected
assert (expected / "config.json").is_file()
assert all((expected / name).is_dir() for name in ["history", "studio_history", "data", "presets", "generated_files", ".backups"])
assert all(not p.get("api_key") for p in cfg["profiles"].values())
cm.save_chat_history("check", [{"role":"user", "content":"saved"}])
cm.load_config()
assert cm.load_chat_history("check")[0]["content"] == "saved"
'''
            result = subprocess.run([sys.executable, "-B", "-c", code], env=env, capture_output=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_selected_files_and_directory_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "uploads"
            allowed.mkdir()
            inside = allowed / "file.txt"
            inside.write_text("attachment")
            sibling = root / "uploads-private"
            sibling.mkdir()
            secret = sibling / "secret.txt"
            secret.write_text("private")
            policy = FileAccess((allowed,))
            self.assertTrue(policy.allowed(str(inside)))
            self.assertFalse(policy.allowed(str(secret)))
            self.assertFalse(policy.allowed(str(allowed / ".." / "uploads-private" / "secret.txt")))
            policy.grant(str(secret))
            self.assertTrue(policy.allowed(str(secret)))

    def test_secret_requires_explicit_clear(self):
        self.assertEqual(retained_secret("", "saved"), "saved")
        self.assertEqual(retained_secret("replacement", "saved"), "replacement")
        self.assertEqual(retained_secret("", "saved", True), "")


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="kid-security-")
        cls.user_dir = Path(cls.temporary.name) / "profile"
        cls.user_dir.mkdir()
        cls.old_override = os.environ.get("KID_CONFIG_DIR")
        os.environ["KID_CONFIG_DIR"] = str(cls.user_dir)
        (cls.user_dir / "config.json").write_text(json.dumps({
            "active_profile": "main", "proxy_mode": "direct",
            "search_api_key": "private-search-key",
            "profiles": {"main": {
                "base_url": "https://example.invalid/v1", "api_key": "private-provider-key",
                "model": "test", "is_coder": True, "coder_rules": "legacy",
            }},
        }), encoding="utf-8")
        cls.app = importlib.import_module("desktop_app")
        if cls.app.USER_DATA_DIR != cls.user_dir:
            raise RuntimeError("Tests must run in a fresh Python process with isolated storage")
        cls.app.get_live_balance = lambda *args, **kwargs: None
        cls.server = cls.app.http.server.ThreadingHTTPServer(("127.0.0.1", 0), cls.app.BridgeHandler)
        cls.old_port = cls.app.BRIDGE_PORT
        cls.app.BRIDGE_PORT = cls.server.server_port
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=2)
        cls.app.BRIDGE_PORT = cls.old_port
        if cls.old_override is None:
            os.environ.pop("KID_CONFIG_DIR", None)
        else:
            os.environ["KID_CONFIG_DIR"] = cls.old_override
        cls.temporary.cleanup()

    def request(self, path, *, method="GET", data=None, authenticated=True, headers=None):
        outgoing = {"X-Kid-Token": self.app.BRIDGE_TOKEN} if authenticated else {}
        outgoing.update(headers or {})
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            outgoing["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=outgoing)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_missing_token_foreign_origin_and_host_are_rejected(self):
        self.assertEqual(self.request("/api/init", authenticated=False)[0], 403)
        self.assertEqual(self.request("/api/init", headers={"X-Kid-Token": "\u00e9"})[0], 403)
        self.assertEqual(self.request("/api/init", headers={"Origin": "https://attacker.example"})[0], 403)
        self.assertEqual(self.request("/api/init", headers={"Host": "attacker.example"})[0], 403)

    def test_settings_never_return_saved_keys(self):
        status, headers, body = self.request("/api/init")
        self.assertEqual(status, 200)
        self.assertNotIn(b"private-provider-key", body)
        self.assertNotIn(b"private-search-key", body)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        persona = json.loads(body)["personas"][0]
        self.assertEqual(persona["apiKey"], "")
        self.assertTrue(persona["hasApiKey"])

    def test_arbitrary_local_file_is_denied(self):
        path = quote(str(self.user_dir / "config.json"))
        self.assertEqual(self.request("/api/download-local?path=" + path)[0], 404)

    def test_normal_chat_still_works_and_legacy_flags_do_not_enable_code_execution(self):
        with patch.object(self.app, "user_profile_memory", None), patch.object(
            self.app.api_client, "call_chat_api", return_value={"content": "mock-chat-reply"}
        ):
            status, _, body = self.request("/api/chat", method="POST", data={
                "session_id": "test-chat", "persona_id": "main", "message": "Hello",
                "is_coder": True, "architect_mode": True, "max_steps": 1,
            })
        self.assertEqual(status, 200, body)
        self.assertIn(b"mock-chat-reply", body)
        self.assertNotIn("coder_agent", sys.modules)

    def test_generated_file_is_downloadable_and_scripts_cannot_be_opened(self):
        directory = self.user_dir / "generated_files"
        (directory / "check.txt").write_text("download-test")
        (directory / "check.py").write_text("raise RuntimeError('must not run')")
        index = {"safe": {"stored_name": "check.txt", "download_name": "check.txt"},
                 "script": {"stored_name": "check.py", "download_name": "check.py"}}
        (directory / "index.json").write_text(json.dumps(index))
        status, _, body = self.request("/api/generated/safe")
        self.assertEqual((status, body), (200, b"download-test"))
        self.assertEqual(self.request("/api/generated/script/open", method="POST")[0], 403)
        self.assertNotEqual(self.request("/api/generated/script/open")[0], 200)

    def test_invalid_and_oversized_requests_are_rejected_before_reading(self):
        self.assertEqual(self.request("/api/chat", method="POST", headers={"Content-Length": "-1"})[0], 400)
        self.assertEqual(self.request("/api/chat", method="POST", headers={"Content-Length": str(9 * 1024 * 1024)})[0], 413)

    def test_profile_edits_keep_saved_key_and_explicit_clear_removes_it(self):
        cfg = self.app.load_config()
        original = cfg["profiles"]["main"]["api_key"]
        payload = {"persona": {"id": "main", "name": "Edited", "apiKey": ""}}
        self.assertEqual(self.request("/api/save_personas", method="POST", data=payload)[0], 200)
        self.assertEqual(self.app.load_config()["profiles"]["main"]["api_key"], original)
        payload["persona"]["clearApiKey"] = True
        self.assertEqual(self.request("/api/save_personas", method="POST", data=payload)[0], 200)
        cfg = self.app.load_config()
        self.assertEqual(cfg["profiles"]["main"]["api_key"], "")
        cfg["profiles"]["main"]["api_key"] = original
        self.app.save_config(cfg)

    def test_fresh_layout_and_legacy_mode_removal(self):
        for name in ("history", "studio_history", "data", "generated_files", "presets", ".backups"):
            self.assertTrue((self.user_dir / name).is_dir())
        self.assertTrue((self.user_dir / ".env").is_file())
        profile = self.app.load_config()["profiles"]["main"]
        self.assertFalse(any(key == "is_coder" or key.startswith(("coder_", "architect_")) for key in profile))


if __name__ == "__main__":
    unittest.main()
