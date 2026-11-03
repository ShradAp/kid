"""Command, draft ownership and shutdown regressions for local dictation."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from services.voice_input import VoiceInput, normalized


class VoiceInputTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.voice = VoiceInput(Path(self.directory.name))
        self.voice.enabled = True
        self.voice.session_id = "chat-a"
        self.voice.state = "waiting"

    def texts(self):
        return [event["text"] for event in self.voice.snapshot("chat-a")["events"]]

    def test_background_speech_is_not_added_to_draft(self):
        self.voice.consume_text("это обычный разговор")
        self.assertEqual(self.texts(), [])
        self.assertEqual(self.voice.state, "waiting")

    def test_start_and_stop_commands_are_removed_and_tail_is_ignored(self):
        self.voice.consume_text("Начать запись, привет")
        self.voice.consume_text("это проверка конец записи лишний разговор")
        self.voice.consume_text("потом кто-то говорит")
        self.assertEqual(self.texts(), ["привет", "это проверка"])
        self.assertEqual(self.voice.state, "waiting")

    def test_command_requires_word_boundaries(self):
        self.voice.consume_text("переначать записью")
        self.assertEqual(self.voice.state, "waiting")
        self.assertEqual(self.texts(), [])

    def test_stopped_microphone_cannot_write(self):
        self.voice.enabled = False
        self.voice.state = "recording"
        self.voice.consume_text("не добавлять")
        self.assertEqual(self.texts(), [])

    def test_events_are_session_scoped_and_replay_is_cursor_based(self):
        self.voice.consume_text("начать запись первый текст")
        first = self.voice.sequence
        self.voice.consume_text("второй текст")
        self.assertEqual(self.voice.snapshot("chat-b")["events"], [])
        self.assertTrue(self.voice.snapshot("chat-b")["other_session"])
        self.assertEqual([e["text"] for e in self.voice.snapshot("chat-a", first)["events"]],
                         ["второй текст"])
        result = self.voice.configure("chat-b", False)
        self.assertFalse(result["enabled"])
        self.assertTrue(self.voice.enabled)

    def test_shutdown_joins_worker_and_releases_state(self):
        marker = self.voice.model_path / "am" / "final.mdl"
        marker.parent.mkdir(parents=True)
        marker.touch()
        self.voice.enabled = False
        self.voice.state = "off"
        def fake_run():
            with self.voice.lock:
                self.voice.state = "waiting"
            self.voice.stop_event.wait(2)
        with patch.object(self.voice, "_run", side_effect=fake_run):
            self.voice.configure("chat-a", True)
            result = self.voice.configure("chat-a", False)
        self.assertFalse(result["enabled"])
        self.assertEqual(result["state"], "off")
        self.assertFalse(self.voice.thread.is_alive())

    def test_missing_model_and_invalid_requests_do_not_start_mic(self):
        self.voice.enabled = False
        with self.assertRaises(ValueError):
            self.voice.configure("", True)
        with self.assertRaises(ValueError):
            self.voice.configure("chat-a", "yes")
        with self.assertRaises(RuntimeError):
            self.voice.configure("chat-a", True)
        self.assertFalse(self.voice.enabled)
        self.assertIsNone(self.voice.thread)


if __name__ == "__main__":
    unittest.main()
