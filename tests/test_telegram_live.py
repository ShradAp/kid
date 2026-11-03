"""Preview, panel and notification behavior without credentials or external network."""

import threading
import time
import unittest
from unittest.mock import patch, Mock

import test_telegram as fixture
from services.telegram_transport import TelegramError, TelegramTransport


def parameters_are_preview(parameters):
    rows = (parameters.get("reply_markup") or {}).get("inline_keyboard") or []
    return any(str(button.get("callback_data") or "").startswith("turn:stop:") for row in rows for button in row)


class TelegramLiveTests(unittest.TestCase):
    setUp = fixture.TelegramTests.setUp
    wait = fixture.TelegramTests.wait

    def active(self):
        self.bot.client = self.transport
        sid = self.bot.store.state()["session_id"]
        self.bot.current = (sid, 1)
        self.bot.cancel = threading.Event()
        self.bot.live.begin(sid, 1)
        self.bot.live.turn["started"] -= 2
        return sid

    def test_streaming_edits_one_ordinary_message_and_keeps_final_separate(self):
        sid = self.active()
        self.bot.live.update(sid, 1, text="First")
        self.bot.live.tick()
        self.bot.live.next_preview = 0
        self.bot.live.update(sid, 1, text="First and second")
        self.bot.live.tick()
        self.assertEqual(len(self.transport.sent), 1)
        self.assertEqual(len(self.transport.edits), 1)
        self.assertEqual(self.transport.edits[0]["message_id"], 1)
        self.assertIn("First and second", self.transport.edits[0]["text"])
        self.assertEqual(self.transport.sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                         "turn:stop:" + str(self.bot.live.turn["draft_id"]))
        self.assertFalse(self.transport.drafts)
        self.assertFalse(self.bot.store.state()["history"])

    def test_fast_callbacks_coalesce_and_preview_is_escaped_and_bounded(self):
        sid = self.active()
        for _ in range(30):
            self.bot.live.update(sid, 1, text="<script>" + "😀" * 4000)
            self.bot.live.tick()
        self.assertEqual(len(self.transport.sent), 1)
        text = self.transport.sent[0]["text"]
        self.assertIn("<script>", text)
        self.assertNotIn("parse_mode", self.transport.sent[0])
        self.assertLess(len(text.encode("utf-16-le")) // 2, 600)
        self.assertFalse(self.transport.drafts)

    def test_settings_can_disable_preview_without_affecting_panel(self):
        self.active()
        self.bot.settings.update(live_status=False, streaming=False)
        self.bot.live.request_panel()
        self.bot.live.tick()
        self.assertFalse(self.transport.drafts)
        self.assertIn("Панель KID", self.transport.sent[0]["text"])

    def test_preview_failure_does_not_set_delivery_error_or_repeat_uncertain_creation(self):
        self.active()
        original = self.transport.call
        def call(method, **kwargs):
            if method == "sendMessage":
                raise TelegramError()
            return original(method, **kwargs)
        self.transport.call = call
        self.bot.live.tick()
        self.bot.live.next_preview = 0
        self.bot.live.tick()
        self.assertFalse(self.transport.sent)
        self.assertTrue(self.bot.live.preview_error)
        self.assertFalse(self.bot.network_errors)

    def test_panel_command_posts_at_bottom_and_refresh_edits_clicked_message(self):
        self.bot.client = self.transport
        self.codex.status.return_value = {"proxy_state": "verified", "auth_state": "authenticated",
                                         "limits": {"windows": [{"duration_minutes": 300, "remaining_percent": 47}], "stale": True}}
        self.bot._control({"kind": "control", "text": "/panel"})
        self.bot.live.tick()
        first = self.bot.store.state()["panel_message_id"]
        self.assertIn("осталось 47% (данные устарели)", self.transport.sent[0]["text"])
        self.bot._control({"kind": "control", "text": "/panel"})
        self.bot.live.tick()
        self.assertEqual(len(self.transport.sent), 2)
        self.assertNotEqual(self.bot.store.state()["panel_message_id"], first)
        self.bot._control({"kind":"callback", "callback":{"id":"refresh", "data":"panel:refresh", "message":{"message_id":first}}})
        self.bot.live.tick()
        self.assertEqual(self.transport.edits[-1]["message_id"], first)
        self.codex.get.assert_not_called()

    def test_deleted_panel_is_recreated_and_not_modified_is_success(self):
        self.bot.client = self.transport
        self.bot.store.change(lambda state: state.update(panel_message_id=444))
        original = self.transport.call
        def missing(method, **kwargs):
            if method == "editMessageText":
                raise TelegramError(status_code=400, message_missing=True)
            return original(method, **kwargs)
        self.transport.call = missing
        self.bot.live.request_panel(new=False)
        self.bot.live.tick()
        self.assertTrue(self.bot.live.panel_new)
        self.bot.live.next_panel = 0
        self.bot.live.tick()
        self.assertEqual(len(self.transport.sent), 1)
        self.transport.call = lambda *a, **kw: (_ for _ in ()).throw(TelegramError(not_modified=True))
        self.bot.live.request_panel(new=False)
        self.bot.live.tick()
        self.assertFalse(self.bot.live.panel_requested)

    def test_old_stop_button_cannot_cancel_next_turn_and_unauthorized_stop_is_ignored(self):
        sid = self.active()
        old = self.bot.live.turn["draft_id"]
        self.bot.current = (sid, 2)
        self.bot.live.begin(sid, 2)
        self.assertFalse(self.bot.live.stop(old))
        self.assertFalse(self.bot.cancel.is_set())
        current = self.bot.live.turn["draft_id"]
        self.bot.accept_update({"update_id": 10, "stopped_message_generation": {
            "chat": {"id": 999, "type": "private"}, "draft_id": current}})
        self.assertFalse(self.bot.cancel.is_set())
        self.bot.accept_update({"update_id": 11, "stopped_message_generation": {
            "chat": {"id": fixture.OWNER, "type": "private"}, "draft_id": current}})
        self.assertTrue(self.bot.cancel.is_set())
        self.assertEqual(self.bot.store.state()["offset"], 12)

    def test_clear_invalidates_previews_and_old_panel_stop(self):
        self.active()
        old = self.bot.live.turn["draft_id"]
        self.bot.clear()
        self.bot.live.tick()
        self.assertFalse(self.transport.drafts)
        self.assertFalse(self.bot.live.stop(old))

    def test_preview_finishes_as_small_status_and_removes_stop_button(self):
        self.active()
        self.bot.live.tick()
        self.bot.current = None
        self.bot.store.change(lambda state: state.update(last_turn={"operation_id":"telegram_123456_1", "outcome":"завершён", "delivery_ms":10}))
        self.bot.live.tick()
        self.assertEqual(self.transport.edits[-1]["text"], "Готово. Ответ отправлен ниже.")
        self.assertEqual(self.transport.edits[-1]["reply_markup"], {"inline_keyboard":[]})
        self.assertNotIn("preview_message", self.bot.store.state())

    def test_old_panels_are_not_edited_in_background_after_new_messages(self):
        self.bot.client = self.transport
        self.bot.store.change(lambda state: state.update(panel_message_id=444))
        self.bot.live.tick()
        self.assertFalse(self.transport.edits)
        self.bot.store.change(lambda state: state.update(panel_live=True))
        self.bot.accept_update(fixture.message(1, "new message"))
        self.bot.live.tick()
        self.assertFalse(self.transport.edits)

    def test_panel_command_replies_to_command_instead_of_editing_old_panel(self):
        self.bot.store.change(lambda state: state.update(panel_message_id=444))
        self.bot.start()
        update = fixture.message(1, "/panel")
        update["message"]["message_id"] = 777
        self.bot.accept_update(update)
        self.wait(lambda: bool(self.transport.sent))
        self.assertEqual(self.transport.sent[0]["reply_parameters"]["message_id"], 777)
        self.assertFalse(self.transport.edits)
        self.codex.get.assert_not_called()

    def test_preview_stop_is_cancelled_on_ingress_without_waiting_for_network(self):
        self.active()
        callback = {"from":{"id":fixture.OWNER}, "id":"stop", "data":"turn:stop:" + str(self.bot.live.turn["draft_id"]),
                    "message":{"message_id":123,"chat":{"id":fixture.OWNER,"type":"private"}}}
        self.bot.accept_update({"update_id":1,"callback_query":callback})
        self.assertTrue(self.bot.cancel.is_set())
        self.assertFalse(self.transport.callbacks)

    def test_notifications_announce_start_once_and_recovery_once_without_model_calls(self):
        self.bot.client = self.transport
        self.bot.settings["notifications"] = True
        self.bot.last_contact = int(time.time())
        self.bot.live.tick()
        self.bot.live.tick()
        self.assertEqual(len(self.bot.store.state()["outbox"]), 1)
        self.bot.network_errors["getUpdates"] = "offline"
        self.bot.live.tick()
        self.bot.live.outage -= 20
        self.bot.network_errors.clear()
        self.bot.live.tick()
        self.bot.live.tick()
        outgoing = self.bot.store.state()["outbox"]
        self.assertEqual(len(outgoing), 2)
        self.assertIn("восстановлена", outgoing[-1]["text"])
        self.codex.get.assert_not_called()

    def test_preview_failures_do_not_lose_final_answer_or_repeat_codex(self):
        original = self.transport.call
        def call(method, **kwargs):
            if method == "sendMessage" and parameters_are_preview(kwargs):
                raise TelegramError("preview unavailable", 1)
            return original(method, **kwargs)
        self.transport.call = call
        def run(*args, **kwargs):
            kwargs["on_text"]("partial")
            time.sleep(.8)
            kwargs["on_text"]("partial and more")
            return {"reply": "finished"}
        self.codex.get.return_value.run.side_effect = run
        self.bot.start()
        self.bot.accept_update(fixture.message(1))
        self.wait(lambda: bool(self.transport.sent))
        self.assertEqual(self.transport.sent[0]["text"], "finished")
        self.assertEqual(self.codex.get.return_value.run.call_count, 1)

    def test_clear_button_requires_confirmation_and_old_confirmation_cannot_clear_new_context(self):
        self.bot.client = self.transport
        def click(data):
            self.bot._control({"kind": "callback", "callback": {"id": "click", "data": data}})
        sid = self.bot.store.state()["session_id"]
        self.bot.store.change(lambda s: s["history"].append({"role": "user", "content": "keep until confirmed"}))
        click("panel:clear")
        self.assertTrue(self.bot.store.state()["history"])
        body, markup = self.bot.live.panel(self.bot.store.state(), None)
        self.assertIn("Сообщения в Telegram останутся", body)
        confirm = markup["inline_keyboard"][-1][0]["callback_data"]
        self.assertLessEqual(len(confirm.encode()), 64)
        click("panel:cancel")
        click(confirm)
        self.assertEqual(self.bot.store.state()["session_id"], sid)
        click("panel:clear")
        click(confirm)
        next_sid = self.bot.store.state()["session_id"]
        self.assertNotEqual(next_sid, sid)
        self.assertFalse(self.bot.store.state()["history"])
        click("panel:clear")
        click(confirm)
        self.assertEqual(self.bot.store.state()["session_id"], next_sid)

    def test_controls_work_even_when_delivery_is_blocked(self):
        self.active()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def call(method, **kwargs):
            if method == "sendMessage":
                entered.set()
                release.wait(2)
                return {"message_id": 1}
            return [] if method == "getUpdates" else True
        self.bot.client = Mock(call=call)
        self.bot._reply("upload-equivalent blocked request")
        delivery = threading.Thread(target=self.bot._deliver, daemon=True)
        live = threading.Thread(target=self.bot.live.run, daemon=True)
        delivery.start()
        live.start()
        self.addCleanup(lambda: (self.bot.stop_event.set(), self.bot._wake(), release.set(), delivery.join(2), live.join(2)))
        self.assertTrue(entered.wait(1))
        self.bot.accept_update(fixture.message(100, "/stop"))
        self.assertTrue(self.bot.cancel.wait(1))


class LiveTransportTests(unittest.TestCase):
    def test_only_known_edit_errors_are_classified_without_leaking_server_text(self):
        for description, unchanged, missing in [("Bad Request: message is not modified", True, False),
                                                 ("Bad Request: message to edit not found", False, True),
                                                 (fixture.TOKEN, False, False)]:
            session = Mock()
            session.post.return_value.status_code = 400
            session.post.return_value.json.return_value = {"description": description}
            with patch("services.telegram_transport.requests.Session", return_value=session):
                with self.assertRaises(TelegramError) as caught:
                    TelegramTransport({"token": fixture.TOKEN}, ".").call("editMessageText")
            self.assertEqual(caught.exception.not_modified, unchanged)
            self.assertEqual(caught.exception.message_missing, missing)
            self.assertNotIn(fixture.TOKEN, str(caught.exception))
