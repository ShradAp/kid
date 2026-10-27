"""Offline security checks. No installed WFP rules or real service requests."""
import base64
import copy
import ctypes
import json
import os
from pathlib import Path
import socket
import socketserver
import sys
import tempfile
import threading
import unittest
import time
import types
from unittest.mock import patch

from codex_proxy import ProxyEndpoint, ProxyUnavailable
from native_codex.broker import Broker
from native_codex.process import ProtectedProcess, command, environment
from native_codex import wfp
from native_codex.probe import verify_transports


class WfpPolicyTests(unittest.TestCase):
    def test_native_search_schema_and_mcp_boundary(self):
        from native_codex.schema import tools
        from native_codex.runtime import NativeRuntime
        names = lambda mode: {item["name"] for item in tools(mode)}
        self.assertTrue({"web_search", "fetch_webpage", "analyze_github_repo"} <= names("kid"))
        self.assertFalse({"web_search", "fetch_webpage", "analyze_github_repo"} & names("codex"))
        self.assertTrue({"windows_command", "windows_write_file", "save_note", "manage_memory"} <= names("codex"))
        runtime = NativeRuntime("root", "profile", ProxyEndpoint.parse("http://127.0.0.1:1"))
        runtime.tool_port = 1234
        context = runtime.thread_context("chat", "kid")
        self.assertEqual(context["sandbox"], "read-only")
        self.assertEqual(context["config"]["mcp_servers"]["kid"]["default_tools_approval_mode"], "approve")
        self.assertTrue(context["config"]["mcp_servers"]["kid"]["required"])

    def test_native_research_gets_codex_callback_and_never_calls_api(self):
        from native_codex.tools import execute
        callback = lambda messages, timeout: "Codex evaluation"
        with patch.dict(sys.modules, {"config_manager": types.SimpleNamespace(load_config=lambda: {})}), \
                patch("research_controller.make_controller") as planner, \
                patch("search_tools.web_search", return_value=([], "No evidence")), \
                patch("api_client.call_chat_api", side_effect=AssertionError("Hidden API dependency")):
            result = execute("web_search", {"query": "bounded research"}, threading.Event(), callback)
            self.assertEqual(result, "No evidence")
            self.assertIs(planner.call_args.kwargs["model_call"], callback)

    def test_exact_app_and_one_loopback_tcp_destination_only(self):
        policy = wfp.describe(sys.executable, 47693)
        allow, block4, block6 = policy["rules"]
        self.assertEqual(allow["layer"], wfp.V4)
        self.assertEqual(allow["conditions"][1:], [(wfp.ADDRESS, 3, 0x7F000001),
                                                  (wfp.PORT, 2, 47693), (wfp.PROTOCOL, 1, 6)])
        for rule, layer in ((block4, wfp.V4), (block6, wfp.V6)):
            self.assertEqual(rule["action"], wfp.BLOCK)
            self.assertEqual(rule["layer"], layer)
            self.assertEqual(len(rule["conditions"]), 1)
            self.assertLess(rule["weight"], allow["weight"])
        self.assertEqual(len({item["key"] for item in policy["rules"]}), 3)

    def test_readback_rejects_missing_changed_or_weakened_conditions(self):
        rule = wfp.describe(sys.executable, 47693)["rules"][0]
        app = b"test-app-identity\x00"
        current, keepalive = wfp._make_filter(rule, app)
        self.assertTrue(wfp._equal_filter(current, rule, app))
        current.flags |= 0x40
        self.assertTrue(wfp._equal_filter(current, rule, app))
        current.flags |= 0x20
        self.assertFalse(wfp._equal_filter(current, rule, app))
        current.flags = wfp.PERSISTENT
        for field, value in (("flags", 0), ("count", 1)):
            original = getattr(current, field)
            setattr(current, field, value)
            self.assertFalse(wfp._equal_filter(current, rule, app))
            setattr(current, field, original)
        current.conditions[2].value.value.u16 = 443
        self.assertFalse(wfp._equal_filter(current, rule, app))
        current.conditions[2].value.value.u16 = 47693
        current.action.type = wfp.BLOCK
        self.assertFalse(wfp._equal_filter(current, rule, app))

    def test_paths_ports_and_struct_layout(self):
        self.assertEqual(ctypes.sizeof(wfp.Guid), 16)
        self.assertEqual(ctypes.alignment(wfp.Guid), 4)
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertEqual(ctypes.sizeof(wfp.Filter), 200)
            self.assertEqual(wfp.Filter.conditions.offset, 120)
            self.assertEqual(wfp.Filter.id.offset, 176)
        for port in (0, 443, 65536, True, "47693"):
            with self.assertRaises(wfp.ProtectionUnavailable):
                wfp.describe(sys.executable, port)
        with self.assertRaises(wfp.ProtectionUnavailable):
            wfp.describe("codex.exe", 47693)

    @unittest.skipUnless(os.name == "nt", "Windows security descriptors")
    def test_rule_read_permission_descriptor_is_valid(self):
        # Conversion allocates local memory only; no Windows policy changes.
        with wfp._security_descriptor() as descriptor:
            self.assertTrue(descriptor.value)

    def test_missing_policy_prevents_transport_probes(self):
        with patch("native_codex.probe.verify", side_effect=wfp.ProtectionUnavailable()), \
                patch("native_codex.probe.subprocess.run") as run:
            with self.assertRaises(wfp.ProtectionUnavailable):
                verify_transports("probe.exe", "python", 47693)
            run.assert_not_called()

    def test_timeout_is_not_proof_of_filtering(self):
        from subprocess import CompletedProcess
        result = CompletedProcess([], 0, json.dumps([{"blocked": False}] * 4).encode(), b"")
        with patch("native_codex.probe.verify"), patch("native_codex.probe.subprocess.run", return_value=result), \
                tempfile.TemporaryDirectory() as root:
            with self.assertRaises(wfp.ProtectionUnavailable):
                verify_transports("probe.exe", root, 47693)

    @unittest.skipUnless(os.name == "nt", "Native Windows gate")
    def test_missing_policy_prevents_codex_start_and_broker_registration(self):
        with tempfile.TemporaryDirectory() as root, \
                patch("native_codex.process.metadata", return_value={}), \
                patch("native_codex.process.verify", side_effect=wfp.ProtectionUnavailable()), \
                patch("native_codex.process.subprocess.Popen") as start, \
                patch("native_codex.process.shared_broker") as broker:
            client = ProtectedProcess(root, Path(root) / "profile", ProxyEndpoint.parse("http://127.0.0.1:1"))
            with self.assertRaises(wfp.ProtectionUnavailable):
                client.start("codex")
            start.assert_not_called()
            broker.assert_not_called()

    def test_environment_does_not_inherit_credentials_or_direct_endpoints(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "private", "OPENAI_BASE_URL": "https://direct.invalid",
                                     "NO_PROXY": "*", "CODEX_HOME": "global", "HTTPS_PROXY": "direct"}):
            env = environment("profile", "http://kid:token@127.0.0.1:47693")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("OPENAI_BASE_URL", env)
        self.assertEqual(env["NO_PROXY"], "")
        self.assertEqual(env["HTTPS_PROXY"], "http://kid:token@127.0.0.1:47693")
        self.assertEqual(env["CODEX_HOME"], str(Path("profile") / "native_server"))
        for search in ("codex", "kid"):
            argv = command("codex.exe", search)
            self.assertIn('web_search="live"' if search == "codex" else 'web_search="disabled"', argv)
            for feature in ("shell_tool", "code_mode_host", "browser_use", "system_proxy_fallback"):
                self.assertIn(feature, argv)


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.broker = Broker(port=0)
        self.failed = threading.Event()
        self.authorization, self.url = self.broker.register(ProxyEndpoint.parse("http://127.0.0.1:1"), self.failed.set)

    def tearDown(self):
        self.broker.close()

    def connect(self, authorization=None, destination="api.openai.com:443"):
        connection = socket.create_connection(("127.0.0.1", self.broker.port), timeout=2)
        connection.settimeout(2)
        headers = "" if authorization is None else "Proxy-Authorization: " + authorization + "\r\n"
        connection.sendall((f"CONNECT {destination} HTTP/1.1\r\n" + headers + "\r\n").encode())
        return connection

    def test_unauthenticated_connections_never_dial_external(self):
        with patch("native_codex.broker.connect_via_proxy") as dial:
            for auth in (None, "Basic invalid"):
                with self.connect(auth) as connection:
                    self.assertIn(b"407", connection.recv(4096))
            dial.assert_not_called()
        self.assertFalse(self.failed.is_set())

    def test_authentication_does_not_authorize_arbitrary_network(self):
        with patch("native_codex.broker.connect_via_proxy") as dial:
            for destination in ("example.com:443", "127.0.0.1:443", "api.openai.com:80", "openai.com.attacker.com:443"):
                with self.connect(self.authorization, destination) as connection:
                    self.assertIn(b"403", connection.recv(4096))
            dial.assert_not_called()

    def test_proxy_failure_closes_client_without_direct_retry(self):
        attempts = []
        def broken(endpoint, host, port):
            attempts.append((endpoint.host, endpoint.port, host, port))
            raise ProxyUnavailable()
        with patch("native_codex.broker.connect_via_proxy", side_effect=broken):
            with self.connect(self.authorization) as connection:
                self.assertEqual(connection.recv(4096), b"")
            self.assertTrue(self.failed.wait(2))
            with self.connect(self.authorization) as connection:
                self.assertIn(b"407", connection.recv(4096))
        self.assertEqual(attempts, [("127.0.0.1", 1, "api.openai.com", 443)])

    def test_other_profile_is_not_closed_on_proxy_failure(self):
        failed_other = threading.Event()
        authorization, _ = self.broker.register(ProxyEndpoint.parse("http://127.0.0.1:2"), failed_other.set)
        with patch("native_codex.broker.connect_via_proxy", side_effect=ProxyUnavailable()):
            with self.connect(self.authorization) as connection:
                connection.recv(4096)
            self.assertTrue(self.failed.wait(2))
        with self.connect(authorization, "example.com:443") as connection:
            self.assertIn(b"403", connection.recv(4096))
        self.assertFalse(failed_other.is_set())

    def test_actual_tunnel_relays_data_without_another_socket_destination(self):
        # A real local tunnel; upstream connector is replaced by an already
        # connected socket, so even a regression cannot reach the Internet.
        incoming, remote = socket.socketpair()
        try:
            with patch("native_codex.broker.connect_via_proxy", return_value=incoming) as dial:
                with self.connect(self.authorization) as connection:
                    self.assertIn(b"200", connection.recv(4096))
                    connection.sendall(b"request")
                    remote.settimeout(2)
                    self.assertEqual(remote.recv(4096), b"request")
                    remote.sendall(b"response")
                    self.assertEqual(connection.recv(4096), b"response")
                dial.assert_called_once()
        finally:
            remote.close()


@unittest.skipUnless(os.name == "nt", "Actual Windows commands")
class NativeCommandTests(unittest.TestCase):
    def test_powershell_changes_actual_windows_folder_and_returns_output(self):
        from native_codex.commands import run
        with tempfile.TemporaryDirectory() as root:
            result = run("[IO.File]::WriteAllText('probe.md', 'real-Windows'); Write-Output 'done'",
                         "powershell", root, threading.Event())
            self.assertTrue(result["ok"])
            self.assertEqual((Path(root) / "probe.md").read_text(), "real-Windows")
            self.assertIn("done", result["stdout"])

    def test_cancellation_ends_the_command_supervisor(self):
        from native_codex.commands import run
        with tempfile.TemporaryDirectory() as root:
            cancel = threading.Event()
            timer = threading.Timer(1, cancel.set)
            timer.start()
            started = time.monotonic()
            try:
                result = run("[IO.File]::WriteAllText('child.pid', [string]$PID); Start-Sleep -Seconds 30", "powershell", root, cancel)
            finally:
                timer.cancel()
            self.assertTrue(result["cancelled"])
            self.assertLess(time.monotonic() - started, 4)
            pid = int((Path(root) / "child.pid").read_text())
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if handle:
                try:
                    code = ctypes.c_uint32()
                    self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
                    self.assertNotEqual(code.value, 259, "PowerShell child must actually exit")
                finally:
                    kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
