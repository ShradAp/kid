"""Interactive launches need an actual visible window, not merely exit code zero."""

import threading
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from native_codex.desktop import open_terminal


class DesktopTerminalTests(unittest.TestCase):
    def mocks(self):
        kernel = Mock()
        def system(buffer, length):
            buffer.value = "C:/Windows/System32"
            return len(buffer.value)
        kernel.GetSystemDirectoryW.side_effect = system
        return kernel

    def test_success_requires_unique_visible_window_and_uses_persistent_console(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        def windows():
            from native_codex import desktop
            title = "KID CMD " + desktop.uuid.uuid4().hex[:12]
            return [{"hwnd":987, "pid":999, "title":title}]
        with patch("native_codex.desktop.ctypes.WinDLL", return_value=self.mocks()), \
             patch("native_codex.desktop.uuid.uuid4", return_value=Mock(hex="a" * 32)), \
             patch("native_codex.desktop.subprocess.Popen", return_value=process) as spawn, \
             patch("native_codex.desktop.visible_windows", side_effect=windows):
            result = open_terminal("cmd", threading.Event())
        self.assertTrue(result["ok"] and result["window_visible"])
        self.assertEqual(result["pid"], 123)
        import subprocess
        self.assertEqual(spawn.call_args.kwargs["creationflags"], subprocess.CREATE_NEW_CONSOLE)
        self.assertIn("/k", spawn.call_args.args[0])
        self.assertNotIn("/c", spawn.call_args.args[0])
        process.kill.assert_not_called()

    def test_process_creation_without_visible_window_is_not_success(self):
        process = Mock(pid=123)
        process.poll.return_value = 0
        with patch("native_codex.desktop.ctypes.WinDLL", return_value=self.mocks()), \
             patch("native_codex.desktop.subprocess.Popen", return_value=process), \
             patch("native_codex.desktop.visible_windows", return_value=[]):
            result = open_terminal("cmd", threading.Event())
        self.assertFalse(result["ok"])
        self.assertFalse(result["window_visible"])

    def test_cancellation_and_arbitrary_programs_never_spawn(self):
        cancel = threading.Event()
        cancel.set()
        with patch("native_codex.desktop.subprocess.Popen") as spawn:
            with self.assertRaises(ValueError):
                open_terminal("cmd", cancel)
            for app in ("cmd /c anything", "notepad", "C:/anything.exe"):
                with self.assertRaises(ValueError):
                    open_terminal(app, threading.Event())
        spawn.assert_not_called()

    def test_unavailable_desktop_verification_is_not_reported_as_success(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        with patch("native_codex.desktop.ctypes.WinDLL", return_value=self.mocks()), \
             patch("native_codex.desktop.subprocess.Popen", return_value=process), \
             patch("native_codex.desktop.visible_windows", side_effect=OSError("desktop unavailable")):
            result = open_terminal("cmd", threading.Event())
        self.assertFalse(result["ok"])
        self.assertFalse(result["window_visible"])
        self.assertTrue(result["process_running"])
        self.assertIn("не подтверждено", result["reason"])

    def test_host_launch_needs_active_turn_and_honors_approval_denial(self):
        from application.codex_actions import execute_host_tool
        from tool_execution import ToolExecutionError
        session = Mock()
        session.lock = threading.RLock()
        cancel = threading.Event()
        session.active = {"thread":{"cancel":cancel,"session_id":"telegram_fixture"}}
        session.runtime.native = True
        service = Mock(sessions={"code":("fingerprint", session)})
        service.approvals.wait.return_value = {"decision":"decline"}
        with patch("native_codex.desktop.open_terminal") as launch:
            with self.assertRaises(ToolExecutionError):
                execute_host_tool("windows_open_app", {"app":"cmd"}, "code", cancel,
                                  service=service, workspaces=Mock(), load_config=lambda:{"profiles":{"code":{"engine":"codex"}}},
                                  save_config=Mock(), read_notes=Mock(), prepend_note=Mock())
        launch.assert_not_called()
        service.approvals.wait.assert_called_once()

    def test_opera_launcher_can_exit_when_verified_browser_window_belongs_to_child(self):
        from native_codex.desktop import open_opera_gx
        process = Mock(pid=123)
        process.poll.return_value = 0
        executable = Path("D:/Soft/Opera GX/opera.exe")
        with patch("native_codex.desktop.opera_gx_executable", return_value=executable), \
             patch("native_codex.desktop.visible_windows", side_effect=[[], [{"hwnd":100,"pid":456,"title":"blank"}]]), \
             patch("native_codex.desktop.process_image", return_value=executable.parent / "124.0" / "opera.exe"), \
             patch("native_codex.desktop.subprocess.Popen", return_value=process) as spawn:
            result = open_opera_gx(threading.Event())
        self.assertTrue(result["ok"])
        self.assertEqual(result["pid"], 456)
        self.assertEqual(result["launcher_pid"], 123)
        self.assertEqual(spawn.call_args.args[0][-2:], ["--new-window", "about:blank"])

    def test_existing_or_other_application_window_does_not_prove_opera_opened(self):
        from native_codex.desktop import open_opera_gx
        cancel = threading.Event()
        windows = [{"hwnd":100,"pid":456,"title":"old"}, {"hwnd":200,"pid":789,"title":"Opera GX impostor"}]
        with patch("native_codex.desktop.opera_gx_executable", return_value=Path("D:/Soft/Opera GX/opera.exe")), \
             patch("native_codex.desktop.visible_windows", side_effect=[windows[:1], windows]), \
             patch("native_codex.desktop.process_image", return_value=Path("C:/Windows/notepad.exe")), \
             patch("native_codex.desktop.subprocess.Popen", return_value=Mock(pid=123)), \
             patch.object(cancel, "wait", side_effect=lambda timeout: cancel.set() or True):
            result = open_opera_gx(cancel)
        self.assertFalse(result["ok"])
        self.assertFalse(result["window_visible"])
