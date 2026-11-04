import http.client
import http.server
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from application.lifecycle import ApplicationLifecycle, _BridgeHTTPServer
from isolated_case import IsolatedStorageTestCase


class LocalHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"fixture")

    def log_message(self, *args):
        pass


class LifecycleTests(IsolatedStorageTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="kid-lifecycle-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lifecycle = ApplicationLifecycle(str(self.root / "UI"), self.root / "user", Mock())

    def test_ephemeral_backend_starts_and_shutdown_cancels_once(self):
        worker = threading.Thread(target=self.lifecycle.start_backend, args=(LocalHandler, 0))
        worker.start()
        service, unhook = Mock(), Mock()
        cancel = threading.Event()
        try:
            self.assertTrue(self.lifecycle.bridge_started.wait(5))
            server = self.lifecycle.backend_server
            self.assertIsNotNone(server)
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                self.assertEqual((response.status, response.read()), (200, b"fixture"))
            finally:
                connection.close()
        finally:
            self.assertTrue(self.lifecycle.shutdown(service, {"chat": cancel}, unhook))
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(cancel.is_set())
        self.assertFalse(self.lifecycle.shutdown(service, {}, unhook))
        service.close.assert_called_once_with()
        unhook.assert_called_once_with()
        self.assertIsNone(self.lifecycle.backend_server)

    def test_occupied_bridge_port_signals_failure_without_starting_second_server(self):
        server = _BridgeHTTPServer(("127.0.0.1", 0), LocalHandler)
        self.addCleanup(server.server_close)
        self.lifecycle.start_backend(LocalHandler, server.server_port)
        self.assertTrue(self.lifecycle.bridge_started.is_set())
        self.assertIsNone(self.lifecycle.backend_server)
        self.assertTrue((Path(os.environ["KID_CONFIG_DIR"]) / "last_error.log").is_file())

    def test_frontend_uses_absolute_node_and_owned_environment_without_shell(self):
        ui = self.root / "UI"
        (ui / ".next").mkdir(parents=True)
        (ui / ".next" / "BUILD_ID").write_text("fixture")
        next_cli = ui / "node_modules" / "next" / "dist" / "bin" / "next"
        next_cli.parent.mkdir(parents=True)
        next_cli.write_text("fixture")
        with patch("application.lifecycle.shutil.which", return_value="node-fixture.exe"), \
             patch("application.lifecycle.subprocess.Popen") as spawn, \
             patch.dict(os.environ, {"KID_FRONTEND_DEV": ""}):
            self.lifecycle.start_frontend(43001, 43002, "fixture-token")
        self.assertEqual(spawn.call_args.args[0], ["node-fixture.exe", str(next_cli), "start",
                                                  "-p", "43001", "--hostname", "127.0.0.1"])
        self.assertEqual(spawn.call_args.kwargs["env"]["KID_BRIDGE_TOKEN"], "fixture-token")
        self.assertEqual(spawn.call_args.kwargs["env"]["KID_BRIDGE_PORT"], "43002")
        self.assertNotIn("shell", spawn.call_args.kwargs)
        self.assertIs(self.lifecycle.frontend_process, spawn.return_value)

    def test_private_node_starts_when_no_system_node_is_installed(self):
        ui = self.root / 'UI'
        (ui / '.next').mkdir(parents=True)
        (ui / '.next/BUILD_ID').write_text('fixture')
        next_cli = ui / 'node_modules/next/dist/bin/next'
        next_cli.parent.mkdir(parents=True)
        next_cli.write_text('fixture')
        node = self.root / 'private-node'
        (self.root / '.install-state.json').write_text(json.dumps({
            'schema': 1, 'complete': True, 'node': str(node)}), encoding='utf-8-sig')
        with patch('application.lifecycle.shutil.which', return_value=None), \
             patch('application.lifecycle.subprocess.Popen') as spawn:
            self.lifecycle.start_frontend(43001, 43002, 'fixture-token')
        self.assertEqual(spawn.call_args.args[0][0], str(node / 'node.exe'))

    def test_failed_backend_shutdown_still_closes_socket_and_releases_lock(self):
        server = Mock()
        server.shutdown.side_effect = OSError("fixture")
        self.lifecycle.backend_server = server
        self.assertTrue(self.lifecycle.shutdown(Mock(), {}, Mock()))
        server.server_close.assert_called_once_with()
        self.assertTrue(self.lifecycle.cleanup_lock.acquire(blocking=False))
        self.lifecycle.cleanup_lock.release()
