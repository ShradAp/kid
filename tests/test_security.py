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

    def test_history_restores_codex_activity_and_steps(self):
        saved=[{"role":"assistant","content":"Готово","codex_operation":"restored-op",
                "thinking":["Файл создан"],"activity":{"duration_ms":16000,"status":"completed"}}]
        with patch.object(self.app,"load_history",return_value={"default":saved}):
            status,_,body=self.request("/api/init")
        self.assertEqual(status,200)
        restored=json.loads(body)["session_messages"]["default"][0]
        self.assertEqual(restored["activity"],saved[0]["activity"])
        self.assertEqual(restored["thinking"],saved[0]["thinking"])

    def test_windows_mcp_callback_creates_real_file_with_approval_and_deduplicates(self):
        from codex_service import ProfileSession, ApprovalQueue
        from codex_engine import LimitsCache
        profile={"engine":"codex","codex":{"proxy":"http://127.0.0.1:1","auto_approve_commands":True}}
        session=ProfileSession("host-test",profile,self.user_dir,LimitsCache(self.user_dir),ApprovalQueue())
        session.local_tools=self.app._codex_local_tool
        session.active["host-thread"]={"session_id":"host-chat","cancel":threading.Event(),"operation_id":"host-operation",
            "on_step":None,"steps":[],"refresh_notes":False}
        desktop=self.user_dir/"fake-desktop"
        desktop.mkdir(exist_ok=True)
        with patch.dict(self.app.CODEX_SERVICE.sessions,{"host-test":("test",session)}), \
             patch.object(self.app,"load_config",return_value={"profiles":{"host-test":profile}}), \
             patch("code_workspace.windows_desktop",return_value=desktop), \
             patch.object(self.app.CODEX_SERVICE.approvals,"wait",return_value={"decision":"accept"}) as approve:
            call={"kind":"tool_call","request":{"session_id":"host-chat","name":"windows_write_file",
                "arguments":{"location":"desktop","file_name":"actual.md","content":"456пока"}}}
            result=session.tool_bridge(call)
            self.assertFalse(result["isError"])
            self.assertEqual((desktop/"actual.md").read_text(encoding="utf-8"),"456пока")
            self.assertEqual(session.tool_bridge(call),result)
            approve.assert_called_once()
            with patch("native_codex.commands.run", return_value={"ok":True,"stdout":"unit-command"}) as run:
                completed=session.tool_bridge({"kind":"tool_call","request":{"session_id":"host-chat","name":"windows_command","arguments":{"command":"Write-Output 'unit-command'"}}})
                self.assertFalse(completed["isError"])
                run.assert_called_once()

    def test_secret_reveal_requires_authenticated_explicit_post_and_known_field(self):
        config = {"profiles":{"one":{"api_key":"saved-test-key", "codex":{"proxy_password":"saved-test-password"}},
                               "two":{"api_key":"other-test-key"}},
                  "search_api_key":"saved-search-key", "proxy_pass":"saved-proxy-password"}
        path = "/api/settings/reveal-secret"
        with patch.object(self.app,"load_config",return_value=config):
            self.assertEqual(self.request(path,method="POST",data={"field":"profile_api_key","profile_id":"one"},authenticated=False)[0],403)
            self.assertEqual(self.request(path,method="POST",data={"field":"unknown"})[0],400)
            self.assertEqual(self.request(path,method="POST",data={"field":"profile_api_key","profile_id":"absent"})[0],404)
            for field, profile, expected in (("profile_api_key","one","saved-test-key"),
                ("profile_api_key","two","other-test-key"),("codex_proxy_password","one","saved-test-password"),
                ("search_api_key",None,"saved-search-key"),("proxy_password",None,"saved-proxy-password")):
                status, headers, body = self.request(path,method="POST",data={"field":field,"profile_id":profile})
                self.assertEqual(status,200)
                self.assertEqual(json.loads(body),{"secret":expected})
                self.assertEqual(headers.get("Cache-Control"),"no-store")

    def test_profile_color_is_persisted_and_limited_to_the_palette(self):
        config = {"profiles":{"color-test":{"engine":"api"}}}
        with patch.object(self.app,"load_config",return_value=config), patch.object(self.app,"save_config") as save:
            for color, expected in (("sage","sage"),("unsupported-css","auto")):
                status, _, body = self.request("/api/save_personas",method="POST",data={"persona":{"id":"color-test","color":color}})
                self.assertEqual(status,200,body)
                self.assertEqual(save.call_args.args[0]["profiles"]["color-test"]["color"],expected)

    def test_selecting_codex_profile_connects_in_background_without_login(self):
        config = {"active_profile":"api", "profiles":{"api":{"engine":"api"},"codex":{"engine":"codex"}}}
        connected = threading.Event()
        with patch.object(self.app,"load_config",return_value=config), patch.object(self.app,"save_config"), \
             patch.object(self.app,"_connect_startup_codex",side_effect=lambda: connected.set()) as connect:
            self.assertEqual(self.request("/api/set_active_profile",method="POST",data={"profile_id":"codex"})[0],200)
            self.assertTrue(connected.wait(2))
            connect.assert_called_once_with()
            connect.reset_mock()
            self.assertEqual(self.request("/api/set_active_profile",method="POST",data={"profile_id":"api"})[0],200)
            connect.assert_not_called()

    def test_live_quota_cache_read_never_starts_network_or_reconnects(self):
        from types import SimpleNamespace
        service = self.app.CODEX_SERVICE
        session = SimpleNamespace(connection=SimpleNamespace(closed=False), closed=threading.Event(),
                                  runtime=SimpleNamespace(stopped=threading.Event()), limits_error=False)
        with patch.object(self.app, "load_config", return_value={"profiles":{"quota-test":{"engine":"codex"}}}), \
             patch.dict(service.sessions, {"quota-test": ("test",session)}), \
             patch.object(service, "action", side_effect=AssertionError("No network")), \
             patch.object(service, "get", side_effect=AssertionError("No runtime startup")):
            service.limits.accept("quota-test", {"rateLimits":{"primary":{"usedPercent":20,"windowDurationMins":300}}})
            path = "/api/codex/limits?profile_id=quota-test"
            status, _, body = self.request(path)
            self.assertEqual(status,200)
            first = json.loads(body)["limits"]
            self.assertEqual(first["windows"][0]["remaining_percent"],80)
            self.assertFalse(first["stale"])
            service.limits.accept("quota-test", {"rateLimits":{"primary":{"usedPercent":35,"windowDurationMins":300}}})
            self.assertEqual(json.loads(self.request(path)[2])["limits"]["windows"][0]["remaining_percent"],65)
            session.runtime.stopped.set()
            stale = json.loads(self.request(path)[2])["limits"]
            self.assertTrue(stale["stale"])
            self.assertEqual(stale["windows"][0]["remaining_percent"],65)
            self.assertEqual(self.request(path, authenticated=False)[0],403)

    def test_arbitrary_local_file_is_denied(self):
        path = quote(str(self.user_dir / "config.json"))
        self.assertEqual(self.request("/api/download-local?path=" + path)[0], 404)

    def test_research_mode_persists_without_changing_search_key(self):
        original = self.app.load_config()["search_api_key"]
        for mode in ("isolated", "adaptive"):
            self.assertEqual(self.request("/api/search-settings", method="POST", data={"search_research_mode": mode})[0], 200)
            cfg = self.app.load_config()
            self.assertEqual(cfg["search_research_mode"], mode)
            self.assertEqual(cfg["search_api_key"], original)
            self.assertEqual(json.loads(self.request("/api/init")[2])["search_research_mode"], mode)
        self.assertEqual(self.request("/api/search-settings", method="POST", data={"search_research_mode": "unknown"})[0], 400)
        self.assertEqual(self.app.load_config()["search_research_mode"], "adaptive")

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

    def test_codex_is_blocked_before_any_api_or_tool_work(self):
        import copy
        original = copy.deepcopy(self.app.load_config())
        cfg = copy.deepcopy(original)
        cfg["profiles"]["main"].update({"engine": "codex", "codex": {
            "proxy": "http://127.0.0.1:1", "proxy_password": "private-proxy-password"}})
        self.app.save_config(cfg)
        try:
            with patch.object(self.app.api_client, "call_chat_api") as chat, patch.object(
                self.app._NO_PROXY_OPENER, "open"
            ) as direct, patch.object(self.app, "get_live_balance") as balance, patch.object(
                self.app.subprocess, "Popen"
            ) as spawn:
                requests = [("/api/chat", "POST", {"persona_id": "main", "prompt": "Hello"}),
                            ("/api/models", "GET", None), ("/api/balance/refresh", "GET", None)]
                requests += [("/api/codex/" + action, "POST", {"profile_id": "main"})
                             for action in ("login", "logout", "refresh", "retry")]
                for path, method, data in requests:
                    status, _, body = self.request(path, method=method, data=data)
                    self.assertEqual(status, 503, (path, body))
                    self.assertEqual(json.loads(body)["error"], "Прокси недоступен. Запрос Codex заблокирован")
                    self.assertNotIn(b"private-proxy-password", body)
                status, _, body = self.request("/api/init")
                self.assertEqual(status, 200)
                self.assertIsNone(json.loads(body)["balance"])
                self.assertNotIn(b"private-proxy-password", body)
                for mock in (chat, direct, balance, spawn):
                    mock.assert_not_called()
                self.assertNotIn("default", self.app.CANCEL_EVENTS)
        finally:
            self.app.save_config(original)

    def test_codex_profile_roundtrip_keeps_api_and_other_profiles(self):
        import copy
        original = copy.deepcopy(self.app.load_config())
        try:
            cfg = copy.deepcopy(original)
            cfg["profiles"]["other"] = {"engine": "codex", "codex": {"model": "other-model"}}
            self.app.save_config(cfg)
            payload = {"persona": {"id": "main", "engine": "codex", "codex": {
                "proxy": "http://user@127.0.0.1:8080", "proxy_password": "private-new-password", "search": "kid"}}}
            self.assertEqual(self.request("/api/save_personas", method="POST", data=payload)[0], 200)
            saved = self.app.load_config()
            self.assertEqual(saved["profiles"]["main"]["api_key"], original["profiles"]["main"]["api_key"])
            self.assertEqual(saved["profiles"]["other"]["codex"]["model"], "other-model")
            self.assertNotIn(b"private-new-password", self.request("/api/init")[2])
            self.assertEqual(self.request("/api/save_personas", method="POST", data={"persona": {"id": "main", "engine": "api"}})[0], 200)
            self.assertEqual(self.app.load_config()["profiles"]["main"]["codex"]["proxy_password"], "private-new-password")
        finally:
            self.app.save_config(original)

    def test_codex_never_reaches_available_direct_service_when_proxy_is_off(self):
        import copy
        # Actual loopback service is reachable. Unlike a mocked HTTP client,
        # this records a real fallback connection if a routing branch regresses.
        calls = []
        class Canary(self.app.http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data":[]}')
            def do_POST(self):
                calls.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"choices":[{"message":{"content":"unexpected direct call"}}]}')
            def log_message(self, *args):
                pass
        service = self.app.http.server.ThreadingHTTPServer(("127.0.0.1", 0), Canary)
        worker = threading.Thread(target=service.serve_forever, daemon=True)
        worker.start()
        original = copy.deepcopy(self.app.load_config())
        try:
            probe = http.client.HTTPConnection("127.0.0.1", service.server_port, timeout=2)
            probe.request("GET", "/ready")
            self.assertEqual(probe.getresponse().status, 200)
            probe.close()
            calls.clear()
            for proxy in ("", "http://127.0.0.1:1"):
                cfg = copy.deepcopy(original)
                cfg["profiles"]["main"].update({"engine": "codex", "base_url": f"http://127.0.0.1:{service.server_port}/v1",
                                                "codex": {"proxy": proxy}})
                self.app.save_config(cfg)
                for path, method, data in (("/api/models", "GET", None), ("/api/chat", "POST", {"persona_id": "main", "prompt": "test"}),
                                           ("/api/codex/retry", "POST", {"profile_id": "main"})):
                    self.assertEqual(self.request(path, method=method, data=data)[0], 503)
            self.assertEqual(calls, [])
        finally:
            self.app.save_config(original)
            service.shutdown()
            service.server_close()
            worker.join(timeout=2)

    def test_code_workspace_requires_native_selection(self):
        self.assertEqual(self.request("/api/code/files?profile_id=main")[0], 400)
        with patch.object(self.app, "_pick_directory_native", return_value=str(self.user_dir)):
            self.assertEqual(self.request("/api/code/select", method="POST", data={"profile_id": "main"})[0], 200)
        status, _, body = self.request("/api/code/files?profile_id=main")
        self.assertEqual(status, 200)
        self.assertIn("config.json", json.loads(body)["files"])
        self.assertEqual(self.request("/api/code/preview?profile_id=main&path=../outside.txt")[0], 400)


if __name__ == "__main__":
    unittest.main()
