"""Launcher regression checks; no model calls or private configuration."""
import os
from pathlib import Path
import subprocess
import sys
import shutil
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "Windows desktop activation")
class DesktopLaunchTests(unittest.TestCase):
    def setUp(self):
        # Отдельное имя mutex: тест не должен активировать настоящее окно kid.
        directory = tempfile.TemporaryDirectory(prefix="kid-launch-")
        self.addCleanup(directory.cleanup)
        self.isolated_root = directory.name
        shutil.copyfile(Path(__file__).resolve().parents[1] / "process_cleanup.py",
                        Path(directory.name) / "process_cleanup.py")

    def test_duplicate_launch_activates_owner_and_crash_releases_instance(self):
        code = '''
from process_cleanup import desktop_instance
import threading
activated=threading.Event()
print(desktop_instance(activated.set),flush=True)
print(activated.wait(15),flush=True)
threading.Event().wait(30)
'''
        owner = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, cwd=self.isolated_root)
        try:
            self.assertEqual(owner.stdout.readline().strip(), "True")
            duplicate = subprocess.run([sys.executable, "-c",
                "from process_cleanup import desktop_instance; print(desktop_instance(lambda: None))"],
                capture_output=True, text=True, timeout=10, cwd=self.isolated_root)
            self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
            self.assertEqual(duplicate.stdout.strip(), "False")
            self.assertEqual(owner.stdout.readline().strip(), "True")
        finally:
            owner.kill()
            owner.communicate(timeout=10)
        fresh = subprocess.run([sys.executable, "-c",
            "from process_cleanup import desktop_instance; print(desktop_instance(lambda: None))"],
            capture_output=True, text=True, timeout=10, cwd=self.isolated_root)
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertEqual(fresh.stdout.strip(), "True")

    def test_pyw_selects_project_environment_before_importing_app(self):
        import runpy
        from unittest.mock import patch

        root = Path(__file__).resolve().parents[1]
        launcher = runpy.run_path(str(root / "WEB_UI.pyw"))
        with patch.object(sys, "prefix", str(root / "absent-python")), \
             patch("subprocess.Popen") as start:
            launcher["launch"]()
        self.assertEqual(start.call_count, 1)
        self.assertEqual(start.call_args.args[0],
                         [str(root / ".venv" / "Scripts" / "pythonw.exe"), str(root / "WEB_UI.pyw")])

    def test_bridge_port_cannot_be_shared_with_second_server(self):
        result = subprocess.run([sys.executable, "-c", '''
from desktop_app import _BridgeHTTPServer, BridgeHandler
first = _BridgeHTTPServer(('127.0.0.1', 0), BridgeHandler)
try:
 try:
  second = _BridgeHTTPServer(first.server_address, BridgeHandler)
 except OSError:
  pass
 else:
  second.server_close()
  raise AssertionError('Second bridge was allowed to share the port')
finally:
 first.server_close()
'''], env=dict(os.environ, KID_CONFIG_DIR=self.isolated_root),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_startup_uses_protected_service_only_for_active_codex_profile(self):
        result = subprocess.run([sys.executable, "-c", '''
from unittest.mock import patch, Mock
import desktop_app as app
profile = {'engine':'codex','codex':{'proxy':'http://127.0.0.1:1'}}
config = {'active_profile':'chosen','profiles':{'chosen':profile,'other':{'engine':'codex'}}}
with patch.object(app,'load_config',return_value=config), patch.object(app,'CODEX_SERVICE') as service:
 service.action.return_value={'proxy_state':'verified'}
 app._connect_startup_codex()
 service.action.assert_called_once_with('chosen',profile,'refresh')
 service.reset_mock()
 profile['engine']='api'
 app._connect_startup_codex()
 service.action.assert_not_called()
 profile['engine']='codex'
 app.SHUTTING_DOWN.set()
 app._connect_startup_codex()
 service.action.assert_not_called()
 app.SHUTTING_DOWN.clear()
 service.action.side_effect=RuntimeError('private-secret')
 with patch.object(app,'log_event') as log:
  app._connect_startup_codex()
  assert 'private-secret' not in str(log.call_args_list)
'''], env=dict(os.environ, KID_CONFIG_DIR=self.isolated_root),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
