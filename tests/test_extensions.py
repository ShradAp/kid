"""Local library and actual stdio MCP round trips; no account or network needed."""
import copy
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from application.extension_actions import handle
from codex_engine import LimitsCache, ProtocolError
from codex_service import ApprovalQueue, CodexService, ProfileSession
from extensions_mcp import MCPClient
from services import extensions
from test_file_transfer import Request


MCP_FIXTURE = '''import json, os, sys
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request: continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion":"2025-06-18", "capabilities":{"tools":{}}, "serverInfo":{"name":"fixture", "version":"2.1"}}
    elif method == "tools/list":
        tool = {"name":"echo", "description":"Echo text", "inputSchema":{"type":"object", "properties":{"text":{"type":"string"}}}}
        result = {"tools":[tool, tool] if "--duplicate" in sys.argv else [tool]}
    elif method == "tools/call":
        result = {"content":[{"type":"text", "text":request["params"]["arguments"]["text"]}], "isError":False}
    else: result = {}
    print(json.dumps({"jsonrpc":"2.0", "id":request["id"], "result":result}), flush=True)
'''


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.cfg = {"profiles": {"a": {"engine": "codex"}, "b": {"engine": "codex"}}}
        self.save(self.cfg)
        self.sessions = Mock()
        self.sessions.read_metadata.return_value = {"chat": {"personaId": "a", "title": "Existing chat"}}
        self.service = CodexService(self.data)
        self.addCleanup(self.service.close)

    def save(self, cfg):
        self.cfg = copy.deepcopy(cfg)
        (self.data / "config.json").write_text(json.dumps(self.cfg), encoding="utf-8")

    def api(self, body=None, profile=""):
        request = Request("/api/extensions" + ("?profile=" + profile if profile else ""),
                          json.dumps(body).encode() if body is not None else b"")
        handle(request, "POST" if body is not None else "GET", root=self.data,
               load_config=lambda: copy.deepcopy(self.cfg), save_config=self.save,
               service=self.service, sessions=self.sessions)
        return request.status, json.loads(request.wfile.getvalue())

    def skill(self, name="pdf", dependencies=None):
        folder = self.root / name
        folder.mkdir()
        header = "---\nname: " + name + "\ndescription: Read PDF documents\n"
        if dependencies:
            header += "dependencies: " + json.dumps(dependencies) + "\n"
        (folder / "SKILL.md").write_text(header + "---\nFULL_PRIVATE_INSTRUCTION_" + name, encoding="utf-8")
        (folder / "reference.txt").write_text("RESOURCE_" + name, encoding="utf-8")
        return folder

    def install(self, path, kind="skill"):
        body = {"action": "preview", "source": str(path), "type": kind}
        status, preview = self.api(body)
        self.assertEqual(status, 200, preview)
        status, installed = self.api({**body, "action": "install", "fingerprint": preview["fingerprint"]})
        self.assertEqual(status, 200, installed)
        return self.cfg["extensions"][-1]

    def mcp(self, duplicate=False, env=None):
        script = self.root / ("duplicate.py" if duplicate else "fixture.py")
        script.write_text(MCP_FIXTURE, encoding="utf-8")
        path = self.root / ("duplicate.json" if duplicate else "mcp.json")
        path.write_text(json.dumps({"name": "Fixture", "description": "Echo test text", "mcp_servers": {
            "echo": {"command": sys.executable, "args": [str(script)] + (["--duplicate"] if duplicate else []),
                     "env_from": env or {}}}}), encoding="utf-8")
        return path

    def test_profiles_have_independent_persistent_catalogs_and_lazy_instructions(self):
        first = self.install(self.skill("first"))
        second = self.install(self.skill("second"))
        self.assertNotIn("extension_ids", self.cfg["profiles"]["a"])
        self.cfg["profiles"]["a"]["extension_ids"] = [first["id"]]
        self.cfg["profiles"]["b"]["extension_ids"] = [second["id"]]
        self.save(self.cfg)
        path_a, a = extensions.snapshot(self.data, "a", {})
        path_b, b = extensions.snapshot(self.data, "b", {})
        self.assertEqual([item["name"] for item in a["skills"]], ["first"])
        self.assertEqual([item["name"] for item in b["skills"]], ["second"])
        arguments = extensions.attach({"developerInstructions": "base"}, path_a, a)
        self.assertIn("first", arguments["developerInstructions"])
        self.assertNotIn("second", arguments["developerInstructions"])
        self.assertNotIn("FULL_PRIVATE_INSTRUCTION", arguments["developerInstructions"])
        connection = arguments["config"]["mcp_servers"]["kid_skills"]
        client = MCPClient({"command": connection["command"], "args": connection["args"]})
        try:
            self.assertEqual(client.request("tools/list")["tools"][0]["name"], "read_skill")
            value = client.request("tools/call", {"name": "read_skill", "arguments": {"id": a["skills"][0]["id"]}})
            self.assertIn("FULL_PRIVATE_INSTRUCTION_first", value["content"][0]["text"])
            value = client.request("tools/call", {"name": "read_skill", "arguments": {"id": first["id"]}})
            self.assertIn("FULL_PRIVATE_INSTRUCTION_first", value["content"][0]["text"])
            value = client.request("tools/call", {"name": "read_skill", "arguments": {"id": a["skills"][0]["id"], "resource": "reference.txt"}})
            self.assertIn("RESOURCE_first", value["content"][0]["text"])
            with self.assertRaises(ValueError):
                client.request("tools/call", {"name": "read_skill", "arguments": {"id": b["skills"][0]["id"]}})
            with self.assertRaises(ValueError):
                client.request("tools/call", {"name": "read_skill", "arguments": {"id": second["id"]}})
            with self.assertRaises(ValueError):
                client.request("tools/call", {"name": "read_skill", "arguments": {"id": a["skills"][0]["id"], "resource": "../../config.json"}})
        finally:
            client.close()
        # A process restart reads the same frozen catalog from disk.
        self.assertEqual(extensions.snapshot(self.data, "a", {}, path_a)[1], a)
        self.assertTrue(Path(path_b).is_file())
        # A fresh interpreter must restore selection through the real settings loader.
        restored = subprocess.run([sys.executable, "-B", "-c",
            "import json; from config_manager import load_config; print(json.dumps({k:v.get('extension_ids',[]) for k,v in load_config()['profiles'].items()}))"],
            cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, KID_CONFIG_DIR=str(self.data)),
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        selections = json.loads(restored.stdout)
        self.assertEqual(selections["a"], [first["id"]])
        self.assertEqual(selections["b"], [second["id"]])

    def test_source_change_after_preview_requires_new_preview(self):
        folder = self.skill()
        body = {"source": str(folder), "type": "skill"}
        _, preview = self.api({**body, "action": "preview"})
        (folder / "reference.txt").write_text("changed", encoding="utf-8")
        status, result = self.api({**body, "action": "install", "fingerprint": preview["fingerprint"]})
        self.assertEqual(status, 400)
        self.assertIn("изменился", result["error"])
        self.assertFalse(self.cfg.get("extensions"))

    def test_actual_mcp_handshake_and_tool_result_from_registered_connection(self):
        item = self.install(self.mcp(), "mcp")
        self.assertEqual(self.api()[1]["extensions"][0]["status"], "requires_setup")
        status, result = self.api({"action": "probe", "id": item["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(result["probe"]["echo"]["status"], "ready")
        self.cfg["profiles"]["a"]["extension_ids"] = [item["id"]]
        self.save(self.cfg)
        path, snapshot = extensions.snapshot(self.data, "a", {})
        arguments = extensions.attach({"config": {"mcp_servers": {"kid": {"required": True}}}}, path, snapshot)
        self.assertTrue(arguments["config"]["mcp_servers"]["kid"]["required"])
        connection = next(value for key, value in arguments["config"]["mcp_servers"].items() if key.startswith("ext_"))
        self.assertFalse(connection["required"])
        client = MCPClient({key: connection[key] for key in ("command", "args")})
        try:
            response = client.request("tools/call", {"name": "echo", "arguments": {"text": "REAL_MCP_RESULT"}})
            self.assertEqual(response["content"][0]["text"], "REAL_MCP_RESULT")
        finally:
            client.close()

    def test_duplicate_tools_and_failed_mcp_do_not_block_a_skill(self):
        bad = self.install(self.mcp(duplicate=True), "mcp")
        self.api({"action": "probe", "id": bad["id"]})
        good = self.install(self.skill())
        self.cfg["profiles"]["a"]["extension_ids"] = [bad["id"], good["id"]]
        self.save(self.cfg)
        listing = {item["id"]: item for item in self.api(profile="a")[1]["extensions"]}
        self.assertEqual(listing[bad["id"]]["status"], "error")
        self.assertIn("одинаковыми именами", listing[bad["id"]]["reasons"][0])
        _, snapshot = extensions.snapshot(self.data, "a", {})
        self.assertEqual(len(snapshot["skills"]), 1)
        self.assertEqual(snapshot["mcp_servers"], {})

    def test_dependencies_disabled_missing_and_cycles_are_visible(self):
        dependency = self.install(self.skill("dependency"))
        child = self.install(self.skill("child", [dependency["id"]]))
        states = {item["id"]: item for item in extensions.states(self.cfg, [child["id"]])}
        self.assertIn("Включите зависимость", states[child["id"]]["reasons"][0])
        self.assertTrue(all(item["status"] == "ready" for item in extensions.states(self.cfg, [child["id"], dependency["id"]])))
        self.cfg["extensions"][0]["dependencies"] = [child["id"]]
        self.assertTrue(all(item["status"] != "ready" for item in extensions.states(self.cfg, [child["id"], dependency["id"]])))
        self.cfg["extensions"].pop(0)
        self.assertIn("Отсутствует зависимость", extensions.states(self.cfg, [child["id"]])[0]["reasons"][0])

    def test_remove_cleans_all_profiles_and_keeps_running_snapshot(self):
        item = self.install(self.skill())
        for profile in self.cfg["profiles"].values():
            profile["extension_ids"] = [item["id"]]
        self.save(self.cfg)
        path, snapshot = extensions.snapshot(self.data, "a", {})
        status, _ = self.api({"action": "remove", "id": item["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.cfg["extensions"], [])
        self.assertTrue(all(profile["extension_ids"] == [] for profile in self.cfg["profiles"].values()))
        self.assertTrue(Path(snapshot["skills"][0]["path"]).is_file())
        self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8")), snapshot)
        self.assertEqual(extensions.snapshot(self.data, "a", {}, path)[1]["skills"], [])

    def test_package_deduplicates_declared_components(self):
        folder = self.skill()
        direct = self.install(folder)
        package = self.root / "kid-extension.json"
        package.write_text(json.dumps({"format": "kid-extension-v1", "name": "Package", "description": "PDF workflow",
            "skills": ["pdf"], "dependencies": [direct["id"]]}), encoding="utf-8")
        plugin = self.install(package, "plugin")
        self.cfg["profiles"]["a"]["extension_ids"] = [plugin["id"], direct["id"]]
        self.save(self.cfg)
        _, snapshot = extensions.snapshot(self.data, "a", {})
        self.assertEqual(len(snapshot["skills"]), 1)

    def test_unknown_plugin_http_and_install_are_not_reported_ready(self):
        manifest = self.root / "plugin.json"
        manifest.write_text('{"name":"Unknown", "description":"Example"}', encoding="utf-8")
        self.assertEqual(self.api({"action": "preview", "source": str(manifest), "type": "plugin"})[0], 400)
        manifest.write_text('{"format":"kid-extension-v1","name":"Hooks","description":"Example","hooks":{},"skills":[]}', encoding="utf-8")
        self.assertEqual(self.api({"action": "preview", "source": str(manifest), "type": "plugin"})[0], 400)
        manifest.write_text('{"name":"HTTP", "description":"Example", "connection":{"transport":"http", "url":"https://example.test"}}', encoding="utf-8")
        self.assertEqual(self.api({"action": "preview", "source": str(manifest), "type": "mcp"})[0], 400)
        with patch("extensions_mcp.subprocess.Popen") as launch:
            self.install(self.mcp(), "mcp")
        launch.assert_not_called()
        status, listing = self.api(profile="unknown")
        self.assertEqual(status, 200)
        self.assertIn("провайдер", listing["extensions"][0]["reasons"][0])

    def test_secrets_are_masked_and_configure_does_not_launch_programs(self):
        item = self.install(self.mcp(env={"API_KEY": "KID_FIXTURE_KEY"}), "mcp")
        with patch("extensions_mcp.subprocess.Popen") as launch:
            status, result = self.api({"action": "configure", "id": item["id"], "secrets": {"KID_FIXTURE_KEY": "SECRET_FIXTURE_123"}})
        self.assertEqual(status, 200, result)
        launch.assert_not_called()
        self.assertNotIn("SECRET_FIXTURE_123", json.dumps(self.api()[1]))
        self.assertNotIn("SECRET_FIXTURE_123", json.dumps(extensions.states(self.cfg)))
        self.assertEqual(self.cfg["extension_secrets"]["KID_FIXTURE_KEY"], "SECRET_FIXTURE_123")
        self.api({"action": "configure", "id": item["id"], "secrets": {"KID_FIXTURE_KEY": ""}})
        self.assertEqual(self.cfg["extension_secrets"]["KID_FIXTURE_KEY"], "SECRET_FIXTURE_123")
        self.api({"action": "probe", "id": item["id"]})
        self.cfg["profiles"]["a"]["extension_ids"] = [item["id"]]
        self.save(self.cfg)
        _, snapshot = extensions.snapshot(self.data, "a", {})
        self.assertNotIn("SECRET_FIXTURE_123", json.dumps(snapshot))
        self.api({"action": "configure", "id": item["id"], "clear_secrets": ["KID_FIXTURE_KEY"]})
        self.assertEqual(self.cfg["extension_secrets"]["KID_FIXTURE_KEY"], "")

    def test_selection_change_does_not_close_live_profile_session(self):
        self.cfg.update({"proxy_mode": "custom", "proxy_url": "http://127.0.0.1:1234"})
        self.save(self.cfg)
        profile = {"engine": "codex", "codex": {"proxy": "http://127.0.0.1:1"}, "extension_ids": []}
        first = self.service.get("a", profile)
        first.close = Mock()
        second = self.service.get("a", {**profile, "extension_ids": ["new"]})
        self.assertIs(first, second)
        first.close.assert_not_called()

    def test_checkbox_endpoint_saves_only_selected_profile_without_touching_active_turn(self):
        item = self.install(self.skill())
        service_calls = Mock()
        self.service.get = service_calls
        status, result = self.api({"action": "select", "profile_id": "a", "selected": [item["id"]]})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["apply"], "next_message")
        self.assertEqual(self.cfg["profiles"]["a"]["extension_ids"], [item["id"]])
        self.assertEqual(self.cfg["profiles"]["b"].get("extension_ids", []), [])
        service_calls.assert_not_called()
        self.api({"action": "select", "profile_id": "a", "selected": []})
        self.assertEqual(self.cfg["profiles"]["a"]["extension_ids"], [])

    def test_idle_restart_preserves_history_binding_replay_and_rejects_busy(self):
        session = ProfileSession("a", {"codex": {"proxy": "http://127.0.0.1:1"}}, self.data,
                                 LimitsCache(self.data), ApprovalQueue())
        session.bindings.write("chat", {"thread_id": "original", "status": "completed", "operation_id": "op",
                                       "result": {"reply": "answer"}, "extension_snapshot": "old"})
        lock = session.session_locks["chat"]
        lock.acquire()
        try:
            with self.assertRaises(ProtocolError):
                session.restart_extensions("chat")
        finally:
            lock.release()
        self.assertEqual(session.bindings.read("chat")["extension_snapshot"], "old")
        session.restart_extensions("chat")
        binding = session.bindings.read("chat")
        self.assertEqual(binding["status"], "abandoned")
        self.assertEqual(binding["operations"]["op"]["status"], "completed")
        self.assertEqual(binding["operations"]["op"]["result"], {"reply": "answer"})
        self.assertEqual(binding["thread_id"], "original")

    def test_native_thread_refreshes_selection_between_messages_and_suppresses_implicit_catalogs(self):
        first = self.install(self.skill("first"))
        second = self.install(self.skill("second"))
        self.cfg["profiles"]["a"]["extension_ids"] = [first["id"]]
        self.save(self.cfg)
        session = ProfileSession("a", {"codex": {"proxy": "http://127.0.0.1:1"}}, self.data,
                                 LimitsCache(self.data), ApprovalQueue())
        session.start = Mock()
        session.account = {"type": "chatgpt"}
        session.runtime.tool_port = 1234
        session.history_reader = Mock(return_value=[{"role": "user", "content": "KEEP_HISTORY"}])
        calls = []
        thread_count = [0]
        def rpc(method, params=None, **kwargs):
            calls.append((method, copy.deepcopy(params)))
            if method == "config/read":
                return {"config": {"mcp_servers": {"unselected_global": {"command": "forbidden"}}, "plugins": {"global_plugin": {"enabled": True}}}}
            if method == "skills/list":
                return {"data": [{"skills": [{"path": str(self.root / "GLOBAL_SKILL.md")}]}]}
            if method in ("thread/start", "thread/resume", "thread/fork"):
                if method in ("thread/start", "thread/fork"): thread_count[0] += 1
                return {"thread": {"id": "thread-" + str(thread_count[0])}}
            if method == "turn/start":
                if thread_count[0] == 1:
                    self.cfg["profiles"]["a"]["extension_ids"] = [second["id"]]
                    self.save(self.cfg)
                    with self.assertRaises(ProtocolError): session.restart_extensions("chat")
                    pinned = session.bindings.read("chat")["extension_snapshot"]
                    self.assertEqual(json.loads(Path(pinned).read_text(encoding="utf-8"))["selection"], [first["id"]])
                    self.assertFalse(session.active[params["threadId"]]["cancel"].is_set())
                session.notify("turn/completed", {"threadId": params["threadId"], "turn": {"id": "turn", "status": "completed"}})
                return {"turn": {"id": "turn"}}
            return {}
        session.rpc = Mock(side_effect=rpc)
        with patch("memory.user_profile.build_memory_context", return_value=""):
            session.run("chat", "hello", "op1", threading.Event())
            snapshot_path = session.bindings.read("chat")["extension_snapshot"]
            session.run("chat", "next", "op2", threading.Event())
            self.assertNotEqual(session.bindings.read("chat")["extension_snapshot"], snapshot_path)
            self.assertEqual(thread_count[0], 2)
            session.run("chat", "continue", "op3", threading.Event())
        threads = [params for method, params in calls if method in ("thread/start", "thread/resume", "thread/fork")]
        self.assertIn('"name": "first"', threads[0]["developerInstructions"])
        self.assertIn('"name": "second"', threads[1]["developerInstructions"])
        self.assertNotIn('"name": "first"', threads[1]["developerInstructions"])
        self.assertEqual(threads[1]["threadId"], "thread-1")
        self.assertIn('"name": "second"', threads[2]["developerInstructions"])
        self.assertEqual(threads[0]["config"]["mcp_servers"]["unselected_global"], {"enabled": False})
        self.assertEqual(threads[0]["config"]["plugins"]["global_plugin"], {"enabled": False})
        self.assertFalse(threads[0]["config"]["skills"]["config"][0]["enabled"])
        self.assertIn("kid", threads[0]["config"]["mcp_servers"])
        self.assertEqual(thread_count[0], 2)
        session.history_reader.assert_not_called()

    def test_profile_save_persists_ids_and_removes_deleted_references(self):
        from application.settings_profiles import save_persona
        item = self.install(self.skill())
        persona = {"id": "a", "extensionIds": [item["id"], "deleted-id", item["id"]]}
        request = Request("/api/save_personas", json.dumps({"persona": persona}).encode())
        save_persona(request, lambda: copy.deepcopy(self.cfg), self.save, self.service)
        self.assertEqual(request.status, 200)
        self.assertEqual(self.cfg["profiles"]["a"]["extension_ids"], [item["id"]])
        self.assertNotIn("extension_ids", self.cfg["profiles"]["b"])

    def test_broken_mcp_timeout_returns_error_without_stalling_other_extensions(self):
        broken = self.root / "broken.py"
        broken.write_text("import time; time.sleep(30)", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "вовремя"):
            MCPClient({"command": sys.executable, "args": [str(broken)]}, timeout=.2)

    def test_declared_skill_mcp_dependency_is_detected_without_text_guessing(self):
        folder = self.skill()
        (folder / "agents").mkdir()
        (folder / "agents" / "openai.yaml").write_text("dependencies:\n  tools:\n    - type: mcp\n      value: echo\n", encoding="utf-8")
        skill = self.install(folder)
        mcp = self.install(self.mcp(), "mcp")
        listing = {item["id"]: item for item in extensions.states(self.cfg, [skill["id"]])}
        self.assertEqual(listing[skill["id"]]["dependency_options"], [mcp["id"]])
        self.assertIn("MCP-зависимость", listing[skill["id"]]["reasons"][0])
        self.api({"action": "probe", "id": mcp["id"]})
        self.assertTrue(all(item["status"] == "ready" for item in extensions.states(self.cfg, [skill["id"], mcp["id"]])))

    def test_duplicate_mcp_components_from_package_and_library_register_once(self):
        mcp = self.install(self.mcp(), "mcp")
        self.api({"action": "probe", "id": mcp["id"]})
        manifest = self.root / "kid-extension.json"
        manifest.write_text(json.dumps({"format": "kid-extension-v1", "name": "Package", "description": "Echo workflow",
            "mcp_servers": mcp["mcp_servers"]}), encoding="utf-8")
        plugin = self.install(manifest, "plugin")
        self.api({"action": "probe", "id": plugin["id"]})
        self.cfg["profiles"]["a"]["extension_ids"] = [mcp["id"], plugin["id"]]
        self.save(self.cfg)
        _, snapshot = extensions.snapshot(self.data, "a", {})
        self.assertEqual(len(snapshot["mcp_servers"]), 1)

    def test_duplicate_manifest_keys_cannot_silently_overwrite_a_server(self):
        manifest = self.root / "duplicate.json"
        manifest.write_text('{"name":"Example", "description":"Example", "mcp_servers":{"same":{},"same":{}}}', encoding="utf-8")
        status, result = self.api({"action": "preview", "source": str(manifest), "type": "mcp"})
        self.assertEqual(status, 400)
        self.assertIn("повторяющиеся", result["error"])

    def test_registration_collision_is_visible_and_does_not_overwrite_existing_tools(self):
        item = self.install(self.mcp(), "mcp")
        self.api({"action": "probe", "id": item["id"]})
        self.cfg["profiles"]["a"]["extension_ids"] = [item["id"]]
        self.save(self.cfg)
        path, snapshot = extensions.snapshot(self.data, "a", {})
        name = next(iter(snapshot["mcp_servers"]))
        original = {"command": "existing-server", "enabled": True}
        arguments = extensions.attach({"config": {"mcp_servers": {name: original, "kid": {"required": True}}}}, path, snapshot)
        self.assertEqual(arguments["config"]["mcp_servers"][name], original)
        self.assertTrue(arguments["config"]["mcp_servers"]["kid"]["required"])
        self.assertEqual(snapshot["errors"][0]["id"], item["id"])
        self.assertIn("Конфликт", snapshot["errors"][0]["error"])

    def test_preparation_failure_disables_dependents_and_keeps_other_extensions(self):
        dependency = self.install(self.skill("dependency"))
        child = self.install(self.skill("child", [dependency["id"]]))
        independent = self.install(self.skill("independent"))
        self.cfg["profiles"]["a"]["extension_ids"] = [dependency["id"], child["id"], independent["id"]]
        self.save(self.cfg)
        original = extensions._copy_skill
        def copy_skill(source, target):
            if str(dependency["id"]) in str(source): raise OSError("fixture unreadable")
            return original(source, target)
        with patch.object(extensions, "_copy_skill", side_effect=copy_skill):
            _, snapshot = extensions.snapshot(self.data, "a", {})
        self.assertEqual([item["name"] for item in snapshot["skills"]], ["independent"])
        self.assertEqual({item["id"] for item in snapshot["errors"]}, {dependency["id"], child["id"]})

    def test_runtime_error_is_local_and_server_payload_never_leaks(self):
        item = self.install(self.mcp(), "mcp")
        session = ProfileSession("a", {"codex": {"proxy": "http://127.0.0.1:1"}}, self.data,
                                 LimitsCache(self.data), ApprovalQueue())
        self.service.sessions["a"] = ("fixture", session)
        session.bindings.write("chat", {"thread_id": "thread-a", "status": "completed"})
        session.notify("mcpServer/startupStatus/updated", {"threadId": "thread-a", "name": "ext_" + item["id"] + "_echo", "status": "failed", "error": "SECRET_FROM_SERVER"})
        listing = self.api(profile="a")[1]
        self.assertEqual(listing["extensions"][0]["status"], "error")
        self.assertNotIn("SECRET_FROM_SERVER", json.dumps(listing))


if __name__ == "__main__":
    unittest.main()
