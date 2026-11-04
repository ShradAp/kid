import copy
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from application.chat_progress import ChatProgress
from bridge_security import FileAccess
from test_file_transfer import Request
from isolated_case import IsolatedStorageTestCase


class CodexChatTests(IsolatedStorageTestCase):
    def setUp(self):
        from application import chat_codex

        self.chat_codex = chat_codex
        self.progress = ChatProgress()
        self.sessions = types.SimpleNamespace(deleted=set(), memory={}, read_metadata=lambda: {})
        self.service = Mock()
        self.result = {"reply": "fixture", "thinking": ["done"], "activity": {"status": "completed"},
                       "images": [], "cancelled": False}
        self.service.get.return_value.run.return_value = self.result

    def run_turn(self, history=None, profile=None):
        request = Request("/api/chat")
        with patch.object(self.chat_codex, "load_chat_history", return_value=copy.deepcopy(history or [])), \
             patch.object(self.chat_codex, "save_chat_history") as save:
            self.chat_codex.run_turn(request, {"prompt": "question", "operation_id": "operation"},
                                "chat", "profile", profile if profile is not None else {"engine": "codex"},
                                service=self.service, sessions=self.sessions,
                                progress=self.progress, file_access=FileAccess())
        self.assertFalse(self.progress.cancel_events)
        self.assertFalse(self.progress.thinking)
        self.assertFalse(self.progress.reply)
        self.assertFalse(self.progress.reasoning)
        return request, save

    def test_success_preserves_operation_history_metadata_and_avoids_duplicate_reply(self):
        request, save = self.run_turn()
        self.assertEqual(request.status, 200)
        self.assertEqual(json.loads(request.wfile.getvalue()), self.result)
        history = save.call_args.args[1]
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])
        self.assertEqual(history[1]["codex_operation"], "operation")
        self.assertEqual(history[1]["activity"], self.result["activity"])
        self.assertIs(self.sessions.memory["chat"], history)
        _, second_save = self.run_turn(history)
        self.assertEqual(second_save.call_args.args[1], history)

    def test_http_profile_timeout_does_not_limit_codex_generation(self):
        for timeout in (30, 300, 1800):
            with self.subTest(timeout=timeout):
                self.run_turn(profile={"engine": "codex", "request_timeout": timeout})
                self.assertIsNone(self.service.get.return_value.run.call_args.kwargs["timeout"])

    def test_drawio_original_reaches_codex_and_attachment_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "схема.drawio")
            path.write_text('<mxfile><diagram>encoded-diagram</diagram></mxfile>', encoding="utf-8")
            request = Request("/api/chat")
            with patch.object(self.chat_codex, "load_chat_history", return_value=[]), \
                 patch.object(self.chat_codex, "save_chat_history") as save:
                self.chat_codex.run_turn(request,
                    {"prompt": "Измени схему", "attachments": [{"path": str(path)}]},
                    "chat", "profile", {"engine": "codex"},
                    service=self.service, sessions=self.sessions, progress=self.progress,
                    file_access=FileAccess((directory,)))
            self.assertEqual(request.status, 200)
            inputs = self.service.get.return_value.run.call_args.kwargs["input_items"]
            self.assertIn(str(path), inputs[0]["text"])
            self.assertEqual(save.call_args.args[1][0]["attachments"][0]["name"], path.name)

    def test_cancel_does_not_persist_turn(self):
        self.result["cancelled"] = True
        request, save = self.run_turn()
        self.assertEqual(request.status, 200)
        save.assert_not_called()
        self.assertFalse(self.sessions.memory)

    def test_internal_error_is_not_reported_as_proxy_failure(self):
        self.service.get.return_value.run.side_effect = RuntimeError("private-secret")
        request, save = self.run_turn()
        message = json.loads(request.wfile.getvalue())["error"]
        self.assertEqual(request.status, 503)
        self.assertIn("Внутренняя ошибка", message)
        self.assertNotIn("Прокси недоступен", message)
        self.assertNotIn("private-secret", message)
        save.assert_not_called()

    def test_proxy_failure_explains_what_needs_fixing(self):
        from codex_proxy import ProxyUnavailable
        self.service.get.side_effect = ProxyUnavailable("Прокси не задан в общих настройках")
        request, save = self.run_turn()
        self.assertIn("Прокси не задан", json.loads(request.wfile.getvalue())["error"])
        save.assert_not_called()

    def test_acknowledged_steers_are_saved_before_final_answer(self):
        self.result["steers"] = [{"role": "user", "content": "clarification", "attachments": []}]
        request, save = self.run_turn()
        self.assertEqual(request.status, 200)
        history = save.call_args.args[1]
        self.assertEqual([item["role"] for item in history], ["user", "user", "assistant"])
        self.assertEqual(history[1]["content"], "clarification")

    def test_steer_endpoint_uses_existing_runtime_and_checks_chat_owner(self):
        session = Mock(profile={})
        self.service.lock = threading.RLock()
        self.service.sessions = {"profile": ("fingerprint", session)}
        self.sessions.read_metadata = lambda: {"chat": {"personaId": "profile"}}
        for profile_id, status in (("other", 409), ("profile", 200)):
            request = Request("/api/chat/steer", json.dumps({"session_id": "chat", "persona_id": profile_id,
                "operation_id": "operation", "prompt": "clarification"}).encode())
            self.chat_codex.steer_turn(request, service=self.service, sessions=self.sessions, file_access=FileAccess())
            self.assertEqual(request.status, status)
        session.steer.assert_called_once_with("chat", "operation", "clarification", [], [])
        self.service.get.assert_not_called()

    def test_unexpected_error_is_sanitized_without_api_fallback(self):
        self.service.get.return_value.run.side_effect = RuntimeError("fixture-private-error")
        with patch("api_client.call_chat_api") as api:
            request, save = self.run_turn()
        self.assertEqual(request.status, 503)
        self.assertNotIn(b"fixture-private-error", request.wfile.getvalue())
        api.assert_not_called()
        save.assert_not_called()

    def test_deleted_session_is_rejected_before_starting_codex(self):
        self.sessions.deleted.add("chat")
        request, save = self.run_turn()
        self.assertEqual(request.status, 503)
        self.service.get.assert_not_called()
        save.assert_not_called()

    def test_overlapping_request_preserves_first_turn_and_stop(self):
        entered, release = threading.Event(), threading.Event()
        first = Request("/api/chat")
        second = Request("/api/chat")
        def generate(*args, **kwargs):
            args[4]("working")
            kwargs["on_text"]("partial answer")
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release generation")
            return {**self.result, "cancelled": True}
        self.service.get.return_value.run.side_effect = generate
        def run(request):
            self.chat_codex.run_turn(request, {"prompt": "question"}, "chat", "profile", {"engine": "codex"},
                service=self.service, sessions=self.sessions, progress=self.progress, file_access=FileAccess())
        worker = threading.Thread(target=run, args=(first,))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            cancel = self.progress.cancel_events["chat"]
            steps = self.progress.thinking["chat"]
            run(second)
            self.assertEqual(second.status, 503)
            self.assertIn("уже выполняется", json.loads(second.wfile.getvalue())["error"])
            self.assertIs(self.progress.cancel_events["chat"], cancel)
            self.assertIs(self.progress.thinking["chat"], steps)
            self.assertEqual(steps, ["working"])
            self.assertEqual(self.progress.reply["chat"], "partial answer")
            stop = Request("/api/chat/stop", json.dumps({"session_id": "chat"}).encode())
            self.progress.stop(stop, lambda text: None)
            self.assertTrue(cancel.is_set())
            self.service.get.return_value.run.assert_called_once()
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.progress.cancel_events)
        self.assertFalse(self.progress.reply)
