"""Telegram boundaries tested with disposable storage and an offline Bot API."""

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from services.telegram_bot import TelegramBot, MAX_AGE
from services.telegram_storage import DEFAULT_SETTINGS
from services.telegram_transport import TelegramError, TelegramTransport, text_chunks


TOKEN = "123456:" + "fixture_secret_" * 3
OWNER = 123456789


def message(identity, text="hello", owner=OWNER, age=0, chat_type="private"):
    return {"update_id": identity, "message": {"from": {"id": owner},
            "chat": {"id": owner, "type": chat_type}, "date": int(time.time()) - age, "text": text}}


class OfflineTelegram:
    def __init__(self, *args):
        self.sent = []
        self.callbacks = []
        self.drafts = []
        self.edits = []
        self.fail_send = threading.Event()

    def call(self, method, **parameters):
        if method == "getUpdates":
            time.sleep(.01)
            return []
        if method in ("sendMessage", "sendRichMessage"):
            if self.fail_send.is_set():
                raise TelegramError("fixture delivery unavailable", 1)
            if method == "sendRichMessage":
                import re
                from html import unescape
                parameters["text"] = unescape(re.sub("<[^>]+>", "", parameters["rich_message"]["html"]))
            self.sent.append(parameters)
            return {"message_id": len(self.sent)}
        if method in ("sendRichMessageDraft", "sendMessageDraft"):
            self.drafts.append(parameters)
            return True
        if method == "editMessageText":
            self.edits.append(parameters)
            return {"message_id": parameters["message_id"]}
        if method == "answerCallbackQuery":
            self.callbacks.append(parameters)
            return True
        raise AssertionError(method)


class TelegramTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="kid-telegram-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.codex = Mock()
        self.codex.approvals.pending.return_value = []
        self.codex.get.return_value.run.return_value = {"reply": "answer", "cancelled": False}
        self.config = {"profiles": {"code": {"engine": "codex"}, "api": {"engine": "api"}}}
        self.transport = OfflineTelegram()
        self.bot = TelegramBot(self.root, self.codex, lambda: self.config,
                               transport_factory=lambda *args: self.transport)
        self.addCleanup(lambda: self.bot.close(timeout=2))
        self.settings = {**DEFAULT_SETTINGS, "enabled": True, "token": TOKEN,
                         "owner_id": str(OWNER), "profile_id": "code", "notifications": False}
        self.bot.store.save_settings(self.settings)
        self.bot.settings = self.settings
        self.bot.store.change(lambda state: None)

    def wait(self, predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail("Timed out waiting for offline worker")

    def test_disabled_module_never_creates_transport(self):
        self.bot.store.save_settings({**self.settings, "enabled": False})
        factory = Mock()
        self.bot.transport_factory = factory
        self.bot.start()
        factory.assert_not_called()
        self.assertFalse(self.bot.threads)

    def test_settings_never_return_token_and_list_only_codex_profiles(self):
        result = self.bot.public_settings()
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertTrue(result["has_token"])
        self.assertEqual([item["id"] for item in result["profiles"]], ["code"])
        self.assertEqual(self.bot.store.settings_path.parent, self.root)
        self.assertEqual(self.bot.store.history_path.parent, self.root / "telegram" / "history")

    def test_unauthorized_users_and_groups_cannot_queue_or_approve(self):
        self.bot.accept_update(message(1, owner=55))
        self.bot.accept_update(message(2, chat_type="group"))
        self.assertEqual(self.bot.store.state()["inbox"], [])
        self.assertEqual(self.bot.store.state()["outbox"], [])
        self.assertEqual(self.bot.store.state()["offset"], 3)
        self.codex.get.assert_not_called()

    def test_duplicate_updates_stay_deduplicated_after_restart(self):
        self.bot.accept_update(message(100))
        other = TelegramBot(self.root, self.codex, lambda: self.config)
        other.settings = self.settings
        other.accept_update(message(100))
        self.assertEqual(len(other.store.state()["inbox"]), 1)
        self.assertEqual(other.store.state()["offset"], 101)

    def test_persistence_failure_does_not_acknowledge_update(self):
        with patch.object(self.bot.store, "write", side_effect=OSError("full disk")):
            with self.assertRaises(OSError):
                self.bot.accept_update(message(22))
        self.assertEqual(self.bot.store.state()["offset"], 0)

    def test_old_messages_are_not_executed(self):
        self.bot.accept_update(message(1, age=MAX_AGE + 10))
        self.assertFalse(self.bot.store.state()["inbox"])
        self.assertIn("старое", self.bot.store.state()["outbox"][0]["text"])

    def test_separate_context_persists_and_calls_existing_codex(self):
        self.bot.start()
        session_id = self.bot.store.state()["session_id"]
        self.bot.accept_update(message(1))
        self.wait(lambda: bool(self.transport.sent))
        args, kwargs = self.codex.get.return_value.run.call_args
        self.assertEqual(args[:3], (session_id, "hello", "telegram_123456_1"))
        self.assertIsNone(kwargs["timeout"])
        self.assertEqual(self.transport.sent[0]["chat_id"], OWNER)
        self.assertEqual(self.bot.history(session_id), [
            {"role": "user", "content": "hello"}, {"role": "assistant", "content": "answer"}])
        self.assertIsNone(self.bot.history("desktop-chat"))

    def test_failed_delivery_retries_answer_without_repeating_codex(self):
        self.transport.fail_send.set()
        self.bot.start()
        self.bot.accept_update(message(1))
        self.wait(lambda: bool(self.bot.store.state()["outbox"]))
        self.transport.fail_send.clear()
        self.wait(lambda: bool(self.transport.sent))
        self.assertEqual(self.codex.get.return_value.run.call_count, 1)

    def test_uncertain_active_turn_is_not_replayed_on_restart(self):
        def active(state):
            state["active"] = {"id": 42, "text": "create file"}
        self.bot.store.change(active)
        self.bot.start()
        self.wait(lambda: bool(self.transport.sent))
        self.assertIn("не повторялся", self.transport.sent[0]["text"])
        self.codex.get.assert_not_called()

    def test_clear_cancels_and_discards_late_answer_and_queued_work(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def run(*args, **kwargs):
            started.set()
            release.wait(3)
            return {"reply": "late answer"}
        self.codex.get.return_value.run.side_effect = run
        self.bot.start()
        old_session = self.bot.store.state()["session_id"]
        self.bot.accept_update(message(1))
        self.assertTrue(started.wait(2))
        self.bot.accept_update(message(2, "queued"))
        self.bot.clear()
        self.assertTrue(self.bot.cancel.is_set())
        self.assertNotEqual(self.bot.store.state()["session_id"], old_session)
        release.set()
        self.wait(lambda: self.bot.current is None)
        state = self.bot.store.state()
        self.assertFalse(state["history"] or state["inbox"] or state["outbox"])
        self.assertFalse(self.transport.sent)
        self.codex.forget.assert_called_with(old_session)
        self.assertEqual(self.codex.get.return_value.run.call_count, 1)

    def test_stop_command_is_processed_while_codex_is_waiting(self):
        started = threading.Event()
        def run(session_id, text, operation, cancel, **kwargs):
            started.set()
            cancel.wait(3)
            return {"reply": "stopped", "cancelled": True}
        self.codex.get.return_value.run.side_effect = run
        self.bot.start()
        self.bot.accept_update(message(1))
        self.assertTrue(started.wait(2))
        self.bot.accept_update(message(2, "queued"))
        self.bot.accept_update(message(3, "/stop"))
        self.wait(lambda: self.bot.current is None)
        self.assertEqual(self.codex.get.return_value.run.call_count, 1)
        self.assertFalse(self.bot.store.state()["inbox"])

    def test_approvals_are_scoped_to_telegram_session_and_owner(self):
        state = self.bot.store.state()
        self.bot.current = (state["session_id"], 1)
        self.codex.approvals.pending.return_value = [
            {"id": "codex_own", "session_id": state["session_id"], "profile_id": "code"},
            {"id": "codex_other", "session_id": "desktop", "profile_id": "code"}]
        self.bot.client = self.transport
        def callback(identity):
            return {"kind": "callback", "callback": {"id": "button", "data": "yes:" + identity}}
        self.bot._control(callback("codex_other"))
        self.codex.approvals.decide.assert_not_called()
        self.bot._control(callback("codex_own"))
        self.codex.approvals.decide.assert_called_once_with("codex_own", True)
        update = {"update_id": 1, "callback_query": {"from": {"id": 55}, "id": "bad", "data": "yes:codex_own",
                  "message": {"chat": {"id": 55, "type": "private"}}}}
        self.bot.accept_update(update)
        self.assertFalse(self.bot.store.state()["inbox"])

    def test_profile_change_with_history_requires_explicit_clear(self):
        self.bot.store.change(lambda state: state["history"].append({"role": "user", "content": "private"}))
        with self.assertRaisesRegex(ValueError, "очистите"):
            self.bot.configure({"profile_id": "other", "enabled": False})
        self.assertEqual(self.bot.store.settings()["profile_id"], "code")

    def test_token_rotation_for_same_bot_preserves_offset(self):
        self.bot.accept_update(message(80, owner=55))
        with patch.object(self.bot, "start"):
            self.bot.configure({"token": "123456:" + "new_secret_" * 3})
        self.assertEqual(self.bot.store.state()["offset"], 81)

    def test_invalid_enabled_settings_do_not_change_saved_settings(self):
        for changes in ({"owner_id": "@username"}, {"profile_id": "api"}, {"token": "bad"}, {"enabled": "true"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.bot.configure(changes)
            self.assertEqual(self.bot.store.settings(), self.settings)

    def test_prompts_execute_in_order_and_completed_answer_is_delivered_before_next_turn(self):
        calls = []
        def run(session_id, text, operation, cancel, **kwargs):
            calls.append(text)
            if text == "second":
                self.assertTrue(any(item["text"] == "first answer" for item in self.transport.sent))
            return {"reply": text + " answer"}
        self.codex.get.return_value.run.side_effect = run
        self.bot.accept_update(message(1, "first"))
        self.bot.accept_update(message(2, "second"))
        self.bot.start()
        self.wait(lambda: len(self.transport.sent) == 2)
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual([item["text"] for item in self.transport.sent], ["first answer", "second answer"])

    def test_successful_poll_does_not_hide_failed_delivery(self):
        self.bot.client = self.transport
        self.transport.fail_send.set()
        with self.assertRaises(TelegramError):
            self.bot._call("sendMessage", text="fixture")
        self.bot._call("getUpdates")
        self.assertIn("delivery unavailable", self.bot.error)
        self.transport.fail_send.clear()
        self.bot._call("sendMessage", text="fixture")
        self.assertFalse(self.bot.error)

    def test_all_controls_in_a_batch_are_processed_without_a_new_update(self):
        self.bot.accept_update(message(1, "/help"))
        self.bot.accept_update(message(2, "/stop"))
        self.bot.accept_update(message(3, "/help"))
        self.bot.start()
        self.wait(lambda: len(self.transport.sent) == 3)
        self.assertFalse(self.bot.store.state()["inbox"])

    def test_turn_metrics_measure_first_text_and_delivery_without_storing_text_in_metrics(self):
        def run(*args, **kwargs):
            kwargs["on_text"]("partial secret text")
            return {"reply": "answer"}
        self.codex.get.return_value.run.side_effect = run
        self.bot.start()
        self.bot.accept_update(message(1))
        self.wait(lambda: (self.bot.store.state().get("last_turn") or {}).get("delivery_ms") is not None)
        metrics = self.bot.public_settings()["last_turn"]
        self.assertLessEqual(metrics["first_text_ms"], metrics["codex_ms"])
        self.assertGreaterEqual(metrics["total_ms"], metrics["codex_ms"])
        self.assertNotIn("partial secret text", json.dumps(metrics))

    def test_idle_worker_waits_for_events_instead_of_rereading_history_on_timer(self):
        self.bot.start()
        time.sleep(.05)
        original = self.bot.store.state
        idle_reads = []
        def track():
            if threading.current_thread().name in ("telegram-codex", "telegram-delivery"):
                idle_reads.append(threading.current_thread().name)
            return original()
        with patch.object(self.bot.store, "state", side_effect=track):
            time.sleep(.3)
        self.assertEqual(idle_reads, [])
        self.bot.accept_update(message(1))
        self.wait(lambda: bool(self.transport.sent))

    def test_ingress_is_not_acknowledged_after_shutdown_has_started(self):
        self.bot.stop_event.set()
        self.bot.accept_update(message(50))
        self.assertEqual(self.bot.store.state()["offset"], 0)
        self.assertFalse(self.bot.store.state()["inbox"])

    def test_approval_button_round_trip_unblocks_codex_without_affecting_desktop_action(self):
        started, allowed = threading.Event(), threading.Event()
        self.addCleanup(allowed.set)
        session_id = self.bot.store.state()["session_id"]
        own = {"id": "codex_button", "session_id": session_id, "profile_id": "code", "command": "fixture command"}
        desktop = {"id": "codex_desktop", "session_id": "desktop", "profile_id": "code", "command": "private desktop command"}
        self.codex.approvals.pending.side_effect = lambda: ([own] if started.is_set() and not allowed.is_set() else []) + [desktop]
        self.codex.approvals.decide.side_effect = lambda identity, decision: allowed.set() or True
        def run(*args, **kwargs):
            started.set()
            allowed.wait(3)
            return {"reply": "approved result"}
        self.codex.get.return_value.run.side_effect = run
        self.bot.start()
        self.bot.accept_update(message(1))
        def is_approval(item):
            return any(str(button.get("callback_data") or "").startswith("yes:")
                       for row in (item.get("reply_markup") or {}).get("inline_keyboard", []) for button in row)
        self.wait(lambda: any(is_approval(item) for item in self.transport.sent))
        approval = next(item for item in self.transport.sent if is_approval(item))
        self.assertNotIn("private desktop", approval["text"])
        data = approval["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.bot.accept_update({"update_id": 2, "callback_query": {"id": "button", "data": data, "from": {"id": OWNER},
                               "message": {"chat": {"id": OWNER, "type": "private"}}}})
        self.wait(lambda: any(item["text"] == "approved result" for item in self.transport.sent))
        self.codex.approvals.decide.assert_called_once_with("codex_button", True)


class TransportTests(unittest.TestCase):
    def test_connections_are_reused_within_a_thread_and_isolated_between_threads(self):
        sessions = []
        def factory():
            client = Mock()
            client.post.return_value.status_code = 200
            client.post.return_value.json.return_value = {"ok": True, "result": {"id": 1}}
            sessions.append(client)
            return client
        transport = TelegramTransport({"token": TOKEN}, ".")
        with patch("services.telegram_transport.requests.Session", side_effect=factory):
            transport.call("getMe")
            transport.call("getMe")
            self.assertEqual(len(sessions), 1)
            thread = threading.Thread(target=lambda: (transport.call("getMe"), transport.close_thread()))
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(sessions), 2)
            sessions[1].close.assert_called_once()
            sessions[0].close.assert_not_called()
            transport.close_thread()
            sessions[0].close.assert_called_once()

    def test_chunking_preserves_emoji_and_text(self):
        original = "😀" * 2500 + "\n" + "a" * 5000
        chunks = list(text_chunks(original))
        self.assertEqual("".join(chunks), original)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 3500 for chunk in chunks))

    def test_transport_masks_exception_urls_and_disables_ambient_proxy_and_redirects(self):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        client.post.side_effect = RuntimeError("https://api.telegram.org/bot" + TOKEN)
        with patch("services.telegram_transport.requests.Session", return_value=client):
            with self.assertRaises(TelegramError) as error:
                TelegramTransport({"token": TOKEN}, ".").call("getUpdates")
        self.assertNotIn(TOKEN, str(error.exception))
        self.assertFalse(client.trust_env)
        self.assertFalse(client.post.call_args.kwargs["allow_redirects"])
        self.assertEqual(client.post.call_args.kwargs["proxies"], {})

    def test_missing_proxy_never_falls_back_to_direct(self):
        with patch("codex_proxy.global_endpoint", side_effect=RuntimeError("fixture")), \
             patch("services.telegram_transport.requests.Session") as session:
            with self.assertRaises(TelegramError):
                TelegramTransport({"token": TOKEN, "network": "proxy"}, ".").call("sendMessage")
            session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
