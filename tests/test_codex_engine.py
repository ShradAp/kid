"""Offline protocol and fail-closed tests. No service/real account is contacted."""
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch

import codex_engine as codex
from code_workspace import CodeWorkspace


class ProfileTests(unittest.TestCase):
    def test_command_auto_approval_is_explicit_boolean_and_profile_scoped(self):
        self.assertFalse(codex.settings({})["auto_approve_commands"])
        profile=codex.update_profile({}, {"codex":{"auto_approve_commands":True}})
        self.assertTrue(codex.public_settings(profile)["auto_approve_commands"])
        self.assertFalse(codex.settings({})["auto_approve_commands"])
        with self.assertRaises(ValueError):
            codex.update_profile(profile,{"codex":{"auto_approve_commands":"true"}})

    def test_legacy_defaults_and_settings_are_independent(self):
        original = {"api_key": "saved-api", "model": "api-model"}
        self.assertEqual(codex.engine(original), "api")
        one = codex.update_profile(original, {"engine": "codex", "codex": {
            "proxy": "http://user@127.0.0.1:8080", "proxy_password": "proxy-secret", "model": "server-model"}})
        self.assertEqual(original, {"api_key": "saved-api", "model": "api-model"})
        self.assertEqual(one["model"], "api-model")
        self.assertEqual(codex.update_profile(one, {"engine": "api"})["codex"], one["codex"])
        other = codex.settings(one)
        other["model"] = "different"
        self.assertEqual(one["codex"]["model"], "server-model")

    def test_password_is_write_only_and_clear_is_explicit(self):
        profile = {"codex": {"proxy_password": "secret", "access_token": "also-secret"}}
        for incoming in ({}, {"proxy_password": ""}):
            result = codex.update_profile(profile, {"codex": incoming})
            self.assertEqual(result["codex"]["proxy_password"], "secret")
        public = json.dumps(codex.public_settings(profile))
        self.assertNotIn("secret", public)
        self.assertNotIn("access_token", public)
        self.assertEqual(codex.update_profile(profile, {"codex": {"clear_proxy_password": True}})["codex"]["proxy_password"], "")

    def test_invalid_proxy_error_never_echoes_credentials(self):
        for url in ("http://u:very-secret@localhost:8080", "http://u:secret@bad:not-a-port", "http://127.0.0.1", "ftp://localhost:20"):
            with self.subTest(url=url), self.assertRaises(ValueError) as error:
                codex.update_profile({}, {"codex": {"proxy": url}})
            self.assertNotIn("secret", str(error.exception))
        public = codex.public_settings({"codex": {"proxy": "http://user:secret@localhost:80"}})
        self.assertEqual(public["proxy"], "")

    def test_profile_home_is_private_and_collision_free(self):
        root = Path(tempfile.gettempdir())
        self.assertNotEqual(codex.profile_home(root, "a/b"), codex.profile_home(root, "a_b"))
        self.assertTrue(codex.profile_home(root, "../../elsewhere").is_relative_to(root / "codex"))

    def test_unknown_engine_never_defaults_to_api(self):
        with self.assertRaises(ValueError):
            codex.engine({"engine": "unknown"})
        with self.assertRaises(ValueError):
            codex.update_profile({}, {"engine": "unknown"})

    def test_isolation_cannot_be_asserted_by_settings(self):
        # All failure scenarios stop before launch, even with retained API keys,
        # a guessed isolation flag and a seemingly reachable proxy endpoint.
        for proxy in ("", "http://127.0.0.1:1", "http://127.0.0.1:8080"):
            profile = {"engine": "codex", "api_key": "saved", "codex": {"proxy": proxy, "verified": True}}
            with self.subTest(proxy=proxy), patch("subprocess.Popen") as spawn, patch("socket.create_connection") as connect:
                for method in ("account/login/start", "account/rateLimits/read", "model/list", "turn/start"):
                    with self.assertRaisesRegex(codex.CodexBlocked, codex.PROXY_BLOCKED):
                        codex.CodexAdapter("one", profile, tempfile.gettempdir()).request(method)
                spawn.assert_not_called()
                connect.assert_not_called()

    def test_research_cannot_use_retained_paid_api(self):
        from research_controller import make_controller
        with patch("research_controller.requests.post") as request:
            with self.assertRaises(codex.CodexBlocked):
                make_controller({"engine": "codex", "api_key": "saved", "base_url": "http://localhost", "model": "old"}, [])
            request.assert_not_called()


class LimitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = codex.LimitsCache(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_fabricated_windows_or_update_time(self):
        self.assertEqual(self.cache.read("one"), {"windows": [], "updated_at": None, "stale": True})

    def test_windows_follow_server_durations_and_profile(self):
        payload = {"rateLimits": {"primary": {"usedPercent": 12, "windowDurationMins": 15}, "secondary": None}}
        self.cache.accept("one", payload, now=100)
        one = self.cache.read("one")
        self.assertEqual(one["windows"][0]["duration_minutes"], 15)
        self.assertEqual(one["windows"][0]["remaining_percent"], 88)
        self.assertIsNone(one["windows"][0]["resets_at"])
        self.assertTrue(one["stale"])
        self.assertEqual(self.cache.read("two")["windows"], [])

    def test_failed_refresh_preserves_last_success(self):
        self.cache.accept("one", {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 20, "resetsAt": 120}}}, now=100)
        with self.assertRaises(ValueError):
            self.cache.accept("one", {})
        data = codex.status("one", {}, self.cache)
        self.assertEqual(data["limits"]["updated_at"], 100)
        self.assertEqual(data["limits"]["windows"][0]["remaining_percent"], 80)

    def test_multiple_buckets_and_invalid_values_are_not_guessed(self):
        self.cache.accept("one", {"rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 10, "windowDurationMins": 300}, "secondary": {"usedPercent": 40, "windowDurationMins": 10080}},
            "other": {"primary": {"usedPercent": float("nan"), "windowDurationMins": True, "resetsAt": "unknown"}},
        }})
        windows = self.cache.read("one")["windows"]
        self.assertEqual([item["duration_minutes"] for item in windows], [300, 10080, None])
        self.assertIsNone(windows[-1]["remaining_percent"])


class FakeStreams:
    def __init__(self):
        self.lines = queue.Queue()
        self.outbound = queue.Queue()
        self.messages = []
        self.auto_reply = True

    def readline(self, limit):
        return self.lines.get(timeout=5)

    def write(self, line):
        message = json.loads(line)
        self.messages.append(message)
        self.outbound.put(message)
        if self.auto_reply and "id" in message and "method" in message:
            self.lines.put(json.dumps({"id": message["id"], "result": {"ok": True}}) + "\n")

    def flush(self):
        pass


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.streams = FakeStreams()
        self.notifications = queue.Queue()
        self.connection = codex.StdioConnection(self.streams, self.streams, lambda method, params: self.notifications.put((method, params)))

    def tearDown(self):
        self.connection.close()
        self.streams.lines.put("")
        self.connection.worker.join(timeout=2)
        self.assertFalse(self.connection.worker.is_alive())

    def test_initialization_order_and_rpc(self):
        self.connection.initialize()
        self.assertEqual([item["method"] for item in self.streams.messages], ["initialize", "initialized"])
        self.assertEqual(self.streams.messages[0]["params"]["clientInfo"]["name"], "kid")
        self.assertTrue(self.connection.request("account/read", {"refreshToken": False})["ok"])

    def test_server_requests_are_denied_not_executed(self):
        self.streams.lines.put(json.dumps({"id": "approval", "method": "item/commandExecution/requestApproval", "params": {"command": "must not execute"}}))
        response = self.streams.outbound.get(timeout=2)
        self.assertEqual(response["id"], "approval")
        self.assertEqual(response["error"]["code"], -32601)

    def test_notifications_do_not_consume_pending_rpc(self):
        self.streams.lines.put(json.dumps({"method": "account/rateLimits/updated", "params": {"rateLimits": {}}}))
        method, _ = self.notifications.get(timeout=2)
        self.assertEqual(method, "account/rateLimits/updated")
        self.assertTrue(self.connection.request("model/list")["ok"])

    def test_timeout_never_replays_action(self):
        self.streams.auto_reply = False
        with self.assertRaises(codex.ProtocolError):
            self.connection.request("turn/start", timeout=.01)
        self.assertEqual(len(self.streams.messages), 1)

    def test_disconnect_releases_waiter_and_redacts_errors(self):
        self.streams.auto_reply = False
        def reply():
            request = self.streams.outbound.get(timeout=2)
            self.streams.lines.put(json.dumps({"id": request["id"], "error": {"message": "https://user:secret@example.com/token"}}))
        worker = threading.Thread(target=reply)
        worker.start()
        with self.assertRaises(codex.ProtocolError) as error:
            self.connection.request("account/read", timeout=2)
        worker.join(timeout=2)
        self.assertNotIn("secret", str(error.exception))
        self.streams.lines.put("")
        self.connection.worker.join(timeout=2)
        with self.assertRaises(codex.ProtocolError):
            self.connection.request("account/read")


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = CodeWorkspace()
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "hello.txt").write_text("hello", encoding="utf-8")
        (self.root / "private.txt").write_text("secret")
        self.workspace.grant("one", self.project)

    def tearDown(self):
        self.tmp.cleanup()

    def test_grant_and_traversal_are_per_profile(self):
        self.assertEqual(self.workspace.files("one")["files"], ["hello.txt"])
        self.assertEqual(self.workspace.preview("one", "hello.txt")["text"], "hello")
        for path in ("../private.txt", str(self.root / "private.txt")):
            with self.assertRaises(ValueError):
                self.workspace.preview("one", path)
        with self.assertRaises(ValueError):
            self.workspace.files("two")
        with self.assertRaises(ValueError):
            CodeWorkspace().files("one")

    def test_preview_is_bounded_and_binary_is_rejected(self):
        (self.project / "large.txt").write_bytes(b"x" * 600_000)
        data = self.workspace.preview("one", "large.txt")
        self.assertTrue(data["truncated"])
        self.assertEqual(len(data["text"]), 512 * 1024)
        (self.project / "binary").write_bytes(b"\0")
        with self.assertRaises(ValueError):
            self.workspace.preview("one", "binary")

    def test_git_cannot_run_on_host_and_requires_isolated_runner(self):
        with patch("code_workspace.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "защищённый транспорт"):
                self.workspace.diff("one", "hello.txt")
            run.assert_not_called()
        called = []
        result = self.workspace.diff("one", "hello.txt", lambda path: called.append(path) or {"text": "diff"})
        self.assertEqual(result["text"], "diff")
        self.assertEqual(called, ["hello.txt"])

    def test_network_paths_are_rejected_before_resolution(self):
        for value in ("//server/share/secret", "\\\\server\\share\\secret", "../outside.txt", "C:/Windows/win.ini"):
            with self.subTest(path=value), patch("code_workspace.Path.resolve") as resolve:
                with self.assertRaises(ValueError):
                    self.workspace.file("one", value)
                resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
