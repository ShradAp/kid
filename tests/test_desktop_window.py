import threading
import types
import unittest
from unittest.mock import Mock, patch

from isolated_case import IsolatedStorageTestCase


class Event:
    def __init__(self):
        self.callbacks = []

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        return self

    def fire(self):
        for callback in self.callbacks:
            callback()


class DesktopWindowTests(IsolatedStorageTestCase):
    def setUp(self):
        from application import desktop_window

        self.module = desktop_window

    def test_window_events_hotkey_and_close_keep_registration_and_cleanup(self):
        window = Mock()
        window.events = types.SimpleNamespace(shown=Event(), minimized=Event(), closed=Event())
        shutdown = Mock(side_effect=[True, False])
        controller = self.module.DesktopWindow(threading.Event(), shutdown)
        with patch.object(self.module.webview, "create_window", return_value=window) as create, \
             patch.object(self.module.webview, "start") as start, \
             patch.object(self.module.keyboard, "add_hotkey") as hotkey, \
             patch.object(self.module.threading, "Thread") as thread, \
             patch.object(self.module.os, "_exit") as exit_process:
            def lifecycle():
                window.events.shown.fire()
                self.assertTrue(controller.ready.is_set())
                toggle = hotkey.call_args.args[1]
                toggle()
                toggle()
                window.events.minimized.fire()
                controller.activate()
                window.events.closed.fire()

            start.side_effect = lifecycle
            controller.run("Fixture", "http://127.0.0.1:43001")
        self.assertEqual(create.call_args.kwargs["url"], "http://127.0.0.1:43001")
        self.assertEqual(create.call_args.kwargs["width"], 1240)
        self.assertEqual(hotkey.call_args.args[0], "ctrl+shift+space")
        self.assertEqual(window.hide.call_count, 2)
        self.assertEqual(window.restore.call_count, 2)
        self.assertEqual(shutdown.call_count, 2)
        exit_process.assert_called_once_with(0)
        thread.return_value.start.assert_called_once_with()

    def test_late_duplicate_activation_during_shutdown_does_not_show_window(self):
        shutting_down = threading.Event()
        controller = self.module.DesktopWindow(shutting_down, Mock())
        controller.window = Mock()
        controller.ready.set()
        shutting_down.set()
        controller.activate()
        controller.window.show.assert_not_called()
        controller.window.restore.assert_not_called()
