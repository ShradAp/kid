"""Turn recovery, tool idempotency and profile isolation without an account."""
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from codex_engine import CodexBlocked, LimitsCache, ProtocolError
from codex_proxy import ProxyUnavailable
from codex_service import ApprovalQueue, CodexService, ProfileSession, SessionBindings, redact
from tool_execution import ExecutionJournal, ToolExecutionError


class SessionTests(unittest.TestCase):
    def setUp(self):
        memory_context = patch("memory.user_profile.build_memory_context", return_value="")
        self.memory_context = memory_context.start()
        self.addCleanup(memory_context.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.session = ProfileSession("one", {"engine": "codex", "codex": {"proxy": "http://127.0.0.1:1"}},
                                      self.directory.name, LimitsCache(self.directory.name), ApprovalQueue())
        self.session.start = Mock()
        self.session.account = {"type": "chatgpt"}
        self.session.runtime.tool_port = 1234
        self.session.connection = types.SimpleNamespace(closed=False)
        self.calls = []
        self.fail = False
        def request(method, params=None, **kwargs):
            self.calls.append((method, params))
            if method in ("thread/start", "thread/resume", "thread/fork"):
                return {"thread": {"id": "thread-one"}}
            if method == "turn/start":
                if self.fail:
                    raise ProtocolError("connection lost")
                self.session.notify("item/agentMessage/delta", {"threadId": params["threadId"], "turnId": "turn-one", "delta": "answer"})
                self.session.notify("turn/completed", {"threadId": params["threadId"], "turn": {"id": "turn-one", "status": "completed"}})
                return {"turn": {"id": "turn-one"}}
            if method == "thread/read":
                return {"thread": {"turns": [{"id": "turn-one", "status": "completed", "items": [
                    {"id": "op-one", "type": "userMessage"}, {"type": "agentMessage", "text": "recovered answer"}]}]}}
            return {}
        self.session.rpc = Mock(side_effect=request)

    def test_default_turn_can_complete_after_two_hours_without_interruption(self):
        clock = [0.0]
        done = Mock()
        original_request = self.session.rpc.side_effect
        waits = []
        def wait_for_completion(timeout):
            waits.append(timeout)
            clock[0] += 3600
            if len(waits) == 1:
                return False
            self.session.notify("item/agentMessage/delta", {"threadId": "thread-one", "turnId": "turn-one", "delta": "long answer"})
            self.session.notify("turn/completed", {"threadId": "thread-one", "turn": {"id": "turn-one", "status": "completed"}})
            return True
        done.wait.side_effect = wait_for_completion
        def request(method, params=None, **kwargs):
            if method == "turn/start":
                self.session.active[params["threadId"]]["done"] = done
                return {"turn": {"id": "turn-one"}}
            return original_request(method, params, **kwargs)
        self.session.rpc.side_effect = request
        with patch("codex_service.time.monotonic", side_effect=lambda: clock[0]):
            result = self.session.run("long-chat", "prompt", "long-op", threading.Event())
        self.assertEqual(result["reply"], "long answer")
        self.assertFalse(result["cancelled"])
        self.assertEqual(result["activity"]["duration_ms"], 7200000)
        self.assertFalse(any(call.args[0] == "turn/interrupt" for call in self.session.rpc.call_args_list))

    def test_manual_stop_still_interrupts_an_unlimited_turn(self):
        cancel = threading.Event()
        cancel.set()
        done = Mock()
        done.wait.side_effect = [False, True]
        original_request = self.session.rpc.side_effect
        def request(method, params=None, **kwargs):
            if method == "turn/start":
                self.session.active[params["threadId"]]["done"] = done
                return {"turn": {"id": "turn-one"}}
            if method == "turn/interrupt":
                self.session.notify("turn/completed", {"threadId": "thread-one", "turn": {"id": "turn-one", "status": "interrupted"}})
                return {}
            return original_request(method, params, **kwargs)
        self.session.rpc.side_effect = request
        result = self.session.run("stop-chat", "prompt", "stop-op", cancel)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["activity"]["status"], "interrupted")
        interrupts = [call for call in self.session.rpc.call_args_list if call.args[0] == "turn/interrupt"]
        self.assertEqual(len(interrupts), 1)

    def test_stream_and_completed_request_are_not_replayed(self):
        texts = []
        result = self.session.run("chat-one", "prompt", "op-one", threading.Event(), on_text=texts.append)
        self.assertEqual(result["reply"], "answer")
        self.assertEqual(texts, ["answer"])
        count = len(self.calls)
        self.assertEqual(self.session.run("chat-one", "prompt", "op-one", threading.Event()), result)
        self.assertEqual(len(self.calls), count)

    def test_steer_targets_current_turn_without_interrupt_and_keeps_history(self):
        original = self.session.rpc.side_effect
        def request(method, params=None, **kwargs):
            if method == "turn/start":
                return {"turn": {"id": "turn-one"}}
            if method == "turn/steer":
                self.assertEqual(params["expectedTurnId"], "turn-one")
                self.assertEqual(params["threadId"], "thread-one")
                self.assertEqual(params["input"][0]["text"], "clarification")
                self.session.notify("item/agentMessage/delta", {"threadId": "thread-one", "turnId": "turn-one", "delta": "updated answer"})
                self.session.notify("turn/completed", {"threadId": "thread-one", "turn": {"id": "turn-one", "status": "completed"}})
                return {"turnId": "turn-one"}
            return original(method, params, **kwargs)
        self.session.rpc.side_effect = request
        bind = self.session._bind_turn
        def bind_and_steer(thread_id, active, turn_id):
            bind(thread_id, active, turn_id)
            with self.assertRaises(ProtocolError):
                self.session.steer("other-chat", "op-one", "wrong chat")
            with self.assertRaises(ProtocolError):
                self.session.steer("chat-one", "old-operation", "late request")
            self.session.steer("chat-one", "op-one", "clarification")
            self.assertFalse(active["cancel"].is_set())
        with patch.object(self.session, "_bind_turn", side_effect=bind_and_steer):
            result = self.session.run("chat-one", "prompt", "op-one", threading.Event())
        self.assertEqual(result["reply"], "updated answer")
        self.assertEqual(result["steers"], [{"role": "user", "content": "clarification", "attachments": []}])
        with self.assertRaises(ProtocolError):
            self.session.steer("chat-one", "op-one", "too late")
        self.assertFalse(any(call.args[0] == "turn/interrupt" for call in self.session.rpc.call_args_list))
        self.assertEqual(sum(call.args[0] == "turn/start" for call in self.session.rpc.call_args_list), 1)

    def test_failed_steer_is_not_replayed_or_added_to_history_and_turn_continues(self):
        active = {"session_id": "chat", "operation_id": "operation", "turn_id": "turn",
                  "cancel": threading.Event(), "done": threading.Event(),
                  "steer_lock": threading.Lock(), "steers": []}
        self.session.active["thread"] = active
        self.session.rpc.side_effect = ProtocolError("uncertain steer")
        with self.assertRaises(ProtocolError):
            self.session.steer("chat", "operation", "clarification")
        self.session.rpc.assert_called_once()
        self.assertFalse(active["cancel"].is_set())
        self.assertEqual(active["steers"], [])

    def test_steer_cannot_submit_when_protected_transport_is_stopped(self):
        self.session.active["thread"] = {"session_id": "chat", "operation_id": "operation", "turn_id": "turn",
            "cancel": threading.Event(), "done": threading.Event(), "steer_lock": threading.Lock(), "steers": []}
        self.session.rpc = ProfileSession.rpc.__get__(self.session)
        self.session.connection = types.SimpleNamespace(request=Mock(), closed=False)
        self.session.runtime.stopped.set()
        with self.assertRaises(CodexBlocked):
            self.session.steer("chat", "operation", "clarification")
        self.session.connection.request.assert_not_called()

    def test_stale_turn_events_before_and_after_start_response_are_ignored(self):
        original = self.session.rpc.side_effect
        texts = []
        def emit(turn_id, text):
            self.session.notify("item/agentMessage/delta", {"threadId": "thread-one", "turnId": turn_id, "delta": text})
            self.session.notify("item/reasoning/summaryTextDelta", {"threadId": "thread-one", "turnId": turn_id, "delta": text})
            self.session.notify("turn/completed", {"threadId": "thread-one", "turn": {"id": turn_id, "status": "completed"}})
        def request(method, params=None, **kwargs):
            if method == "turn/start":
                emit("old-turn", "stale before response")
                emit(None, "unscoped event")
                emit("turn-one", "current")
                self.assertEqual(texts, [])
                return {"turn": {"id": "turn-one"}}
            return original(method, params, **kwargs)
        self.session.rpc.side_effect = request
        bind = self.session._bind_turn
        def bind_with_late_events(thread_id, active, turn_id):
            bind(thread_id, active, turn_id)
            emit("old-turn", "stale after response")
            self.session.notify("item/completed", {"threadId": thread_id, "turnId": "old-turn",
                "item": {"id": "stale", "type": "agentMessage", "text": "stale final"}})
            self.assertEqual(active["turn"]["id"], "turn-one")
        with patch.object(self.session, "_bind_turn", side_effect=bind_with_late_events):
            result = self.session.run("chat", "prompt", "op", threading.Event(), on_text=texts.append)
        self.assertEqual((result["reply"], result["reasoning"]), ("current", "current"))
        self.assertEqual(texts, ["current"])

    def test_stale_completion_cannot_finish_bound_turn(self):
        active = {"turn_id": "new", "turn": {}, "done": threading.Event()}
        self.session.active["thread-one"] = active
        self.session.notify("turn/completed", {"threadId": "thread-one", "turn": {"id": "old", "status": "completed"}})
        self.assertFalse(active["done"].is_set())
        self.assertEqual(active["turn"], {})

    def test_memory_is_refreshed_on_resume_without_changing_thread_identity(self):
        from memory.user_profile import format_facts_block
        self.session.runtime.native = True
        self.session.runtime.thread_context = Mock(return_value={"cwd": str(self.session.home / "working"), "config": {"mcp_servers": {}}})
        self.session.history_reader = Mock(return_value=[])
        block = format_facts_block([{"fact": "Пользователь использует Samsung Galaxy S25 Ultra", "category": "hardware"}])
        self.memory_context.return_value = "\n\n" + block
        self.session.run("chat", "Какой у меня телефон?", "op1", threading.Event())
        arguments = next(params for method, params in self.calls if method == "thread/start")
        first_turn = next(params for method, params in self.calls if method == "turn/start")
        self.assertIn("Samsung Galaxy S25 Ultra", first_turn["input"][0]["text"])
        self.assertIn("Proactively save stable facts", arguments["developerInstructions"])
        self.memory_context.return_value = ""
        self.session.run("chat", "Другое сообщение", "op2", threading.Event())
        resumed = next(params for method, params in self.calls if method == "thread/resume")
        self.assertNotIn("Samsung Galaxy S25 Ultra", resumed["developerInstructions"])
        last_turn = [params for method, params in self.calls if method == "turn/start"][-1]
        self.assertIn("No profile facts retrieved.", last_turn["input"][0]["text"])
        self.assertEqual(resumed["threadId"], "thread-one")
        self.session.history_reader.assert_not_called()
        self.memory_context.assert_called_with("Другое сообщение")

    def test_local_image_is_in_official_turn_input_without_remote_fetch(self):
        image = {"type": "localImage", "path": "C:\\example\\image.png"}
        self.session.run("image-chat", "describe", "image-op", threading.Event(), input_items=[image])
        sent = next(params for method, params in self.calls if method == "turn/start")
        self.assertIn("profile memory snapshot", sent["input"][0]["text"])
        self.assertEqual(sent["input"][1:], [{"type": "text", "text": "describe"}, image])

    def test_generated_image_survives_notifications_cached_result_and_recovery(self):
        import base64
        import io
        from PIL import Image
        stream = io.BytesIO()
        Image.new("RGB", (10, 10), "blue").save(stream, format="PNG")
        item = {"id": "image-one", "type": "imageGeneration", "status": "completed",
                "result": base64.b64encode(stream.getvalue()).decode()}
        original = self.session.rpc.side_effect
        def request(method, params=None, **kwargs):
            if method == "turn/start":
                self.session.notify("item/completed", {"threadId": params["threadId"], "turnId": "turn-one", "item": item})
                # A duplicate completion must not display the image twice.
                self.session.notify("item/completed", {"threadId": params["threadId"], "turnId": "turn-one", "item": item})
            if method == "thread/read":
                return {"thread": {"turns": [{"id": "turn-one", "status": "completed", "items": [item]}]}}
            return original(method, params, **kwargs)
        self.session.rpc.side_effect = request
        result = self.session.run("image-chat", "draw", "image-op", threading.Event())
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(self.session.run("image-chat", "draw", "image-op", threading.Event())["images"], result["images"])
        binding = self.session.bindings.read("image-chat")
        binding["status"] = "uncertain"
        self.session.bindings.write("image-chat", binding)
        recovered = self.session.recover("image-chat")
        self.assertEqual(recovered["images"], result["images"])

    def test_commentary_and_final_items_do_not_duplicate_the_answer(self):
        def request(method, params=None, **kwargs):
            if method == "thread/start": return {"thread":{"id":"phase-thread"}}
            if method == "turn/start":
                for item_id, phase, text in (("progress","commentary","Создаю документ"),("final","final_answer","Документ создан.")):
                    self.session.notify("item/started", {"threadId":"phase-thread","turnId":"phase-turn","item":{"id":item_id,"type":"agentMessage","phase":phase}})
                    self.session.notify("item/agentMessage/delta", {"threadId":"phase-thread","turnId":"phase-turn","itemId":item_id,"delta":text})
                    self.session.notify("item/completed", {"threadId":"phase-thread","turnId":"phase-turn","item":{"id":item_id,"type":"agentMessage","phase":phase,"text":text}})
                self.session.notify("turn/completed", {"threadId":"phase-thread","turnId":"phase-turn","turn":{"status":"completed"}})
                return {"turn":{"id":"phase-turn"}}
            return {}
        self.session.rpc.side_effect=request
        texts=[]
        result=self.session.run("phase-chat","prompt","phase-op",threading.Event(),on_text=texts.append)
        self.assertEqual(result["reply"],"Документ создан.")
        self.assertEqual(result["thinking"],["Создаю документ"])
        self.assertEqual(texts,["Документ создан."])
        self.assertGreaterEqual(result["activity"]["duration_ms"],0)

    def test_phase_omitting_server_keeps_only_last_answer(self):
        active={"text":[],"steps":[],"cancel":threading.Event()}
        self.session._agent_message(active,"first",text="Проверяю файл",completed=True)
        self.session._agent_message(active,"last",delta="Готово")
        self.session._agent_message(active,"last",text="Готово.",completed=True)
        self.assertEqual(active["text"],["Готово."])
        self.assertEqual(active["steps"],["Проверяю файл"])

    def test_auto_approval_is_only_for_commands_in_the_active_uncancelled_turn(self):
        cancel=threading.Event()
        self.session.active["thread-one"]={"session_id":"chat","cancel":cancel}
        self.session.approvals.wait=Mock(return_value={"decision":"decline"})
        params={"threadId":"thread-one","availableDecisions":["accept","decline"]}
        method="item/commandExecution/requestApproval"
        self.assertEqual(self.session.server_request(method,params),{"decision":"decline"})
        self.session.options["auto_approve_commands"]=True
        self.assertEqual(self.session.server_request(method,params),{"decision":"accept"})
        self.assertEqual(self.session.server_request("item/fileChange/requestApproval",params),{"decision":"decline"})
        self.assertEqual(self.session.server_request(method,{**params,"networkApprovalContext":{"host":"example.com"}}),{"decision":"decline"})
        self.assertEqual(self.session.server_request(method,{**params,"additionalPermissions":{"network":True}}),{"decision":"decline"})
        self.assertEqual(self.session.server_request(method,{**params,"availableDecisions":["decline"]}),{"decision":"decline"})
        cancel.set()
        self.assertEqual(self.session.server_request(method,params),{"decision":"cancel"})
        self.assertEqual(self.session.server_request(method,{"threadId":"other"}),{"decision":"cancel"})

    def test_disconnect_cannot_repeat_turn_and_recovery_only_reads(self):
        self.fail = True
        cancelled=threading.Event()
        with self.assertRaises(ProtocolError):
            self.session.run("chat-one", "prompt", "op-one", cancelled)
        self.assertTrue(cancelled.is_set(), "Failed turns must cancel their pending approvals")
        self.assertEqual(self.session.bindings.read("chat-one")["status"], "uncertain")
        recovered = self.session.recover("chat-one")
        self.assertEqual(recovered["reply"], "recovered answer")
        self.assertEqual(sum(method == "turn/start" for method, _ in self.calls), 1)
        resume = next(params for method, params in self.calls if method == "thread/resume")
        self.assertIn("1234", resume["config"]["mcp_servers"]["kid"]["args"])

    def test_new_message_recovers_previous_turn_without_replaying_it(self):
        self.fail = True
        with self.assertRaises(ProtocolError):
            self.session.run("chat-one", "old", "op-one", threading.Event())
        self.fail = False
        result = self.session.run("chat-one", "new", "op-two", threading.Event())
        self.assertEqual(result["reply"], "answer")
        starts = [params for method, params in self.calls if method == "turn/start"]
        self.assertEqual([p["clientUserMessageId"] for p in starts], ["op-one", "op-two"])
        self.assertEqual(self.session.bindings.read("chat-one")["operations"]["op-one"]["status"], "completed")

    def test_unacknowledged_missing_turn_uses_fresh_thread_and_keeps_tombstone(self):
        self.fail = True
        with self.assertRaises(ProtocolError):
            self.session.run("chat-one", "old", "op-one", threading.Event())
        self.fail = False
        original = self.session.rpc.side_effect
        self.session.rpc.side_effect = lambda method, params=None, **kw: (
            {"thread": {"turns": []}} if method == "thread/read" else original(method, params, **kw))
        self.session.run("chat-one", "new", "op-two", threading.Event())
        binding = self.session.bindings.read("chat-one")
        self.assertEqual(binding["operations"]["op-one"]["status"], "abandoned")
        self.assertEqual(sum(method == "thread/start" for method, _ in self.calls), 2)
        with self.assertRaises(ProtocolError):
            self.session.run("chat-one", "old", "op-one", threading.Event())

    def test_new_message_interrupts_known_running_turn_before_starting(self):
        self.session.bindings.write("chat-one", {"thread_id": "thread-one", "turn_id": "old-turn",
            "operation_id": "old-op", "status": "uncertain"})
        original = self.session.rpc.side_effect
        reads = ["inProgress", "interrupted"]
        def request(method, params=None, **kw):
            if method == "thread/read":
                self.calls.append((method, params))
                return {"thread": {"turns": [{"id": "old-turn", "status": reads.pop(0), "items": []}]}}
            return original(method, params, **kw)
        self.session.rpc.side_effect = request
        self.session.run("chat-one", "new", "new-op", threading.Event())
        methods = [method for method, _ in self.calls]
        self.assertLess(methods.index("turn/interrupt"), methods.index("turn/start"))

    def test_failed_validation_does_not_leave_an_uncertain_turn(self):
        self.session.options["effort"] = "unsupported"
        with self.assertRaises(ProtocolError):
            self.session.run("chat-one", "old", "op-one", threading.Event())
        self.assertEqual(self.session.bindings.read("chat-one")["status"], "failed")
        self.session.options["effort"] = ""
        self.session.run("chat-one", "new", "op-two", threading.Event())

    def test_start_retries_setup_with_fresh_runtime_after_proxy_failure(self):
        self.session.start = types.MethodType(ProfileSession.start, self.session)
        self.session.connection = None
        first = Mock(stopped=threading.Event())
        second = Mock(stopped=threading.Event())
        self.session.runtime = first
        self.session.rpc = Mock(return_value={})
        connection = Mock(closed=False)
        with patch("codex_service.probe_exit", side_effect=[ProxyUnavailable(), {"exit_ip": "203.0.113.1"}]), \
             patch("codex_service.runtime_for", return_value=second), \
             patch("codex_service.StdioConnection", return_value=connection), \
             patch("codex_service.time.sleep"), patch.object(self.session, "_poll_limits"):
            self.session.start()
        first.close.assert_called()
        second.start_worker.assert_called_once()
        self.assertFalse(self.session.closed.is_set())
        self.assertIs(self.session.runtime, second)

    def test_proxy_setup_retry_is_bounded(self):
        self.session.start = types.MethodType(ProfileSession.start, self.session)
        with patch.object(self.session, "_start_once", side_effect=CodexBlocked()) as start, \
             patch("codex_service.time.sleep"):
            with self.assertRaises(CodexBlocked):
                self.session.start()
        self.assertEqual(start.call_count, 2)

    def test_service_replaces_closed_transport_without_manual_proxy_check(self):
        service = CodexService(self.directory.name)
        profile = self.session.profile
        self.session.connection.close = Mock()
        with patch("codex_proxy.effective_profile", return_value=profile), \
             patch("codex_service.ProfileSession", return_value=self.session) as factory:
            self.assertIs(service.get("one", profile), self.session)
            self.session.connection.closed = True
            replacement = Mock()
            factory.return_value = replacement
            self.assertIs(service.get("one", profile), replacement)
        self.assertTrue(self.session.closed.is_set())

    def test_status_during_setup_retry_does_not_replace_starting_session(self):
        service = CodexService(self.directory.name)
        entered, release = threading.Event(), threading.Event()
        self.session.start = types.MethodType(ProfileSession.start, self.session)
        self.session.runtime.image_metadata = Mock()
        errors = []
        def setup():
            # State visible while _start_once recreates a failed runtime.
            self.session.closed.set()
            self.session.runtime.stopped.set()
            self.session.connection.closed = True
            entered.set()
            if not release.wait(5):
                raise AssertionError("setup was not released")
            self.session.closed.clear()
            self.session.runtime.stopped.clear()
            self.session.connection.closed = False
        def start():
            try:
                self.session.start()
            except Exception as error:
                errors.append(error)
        with patch("codex_proxy.effective_profile", return_value=self.session.profile), \
             patch("codex_service.ProfileSession", return_value=self.session) as factory, \
             patch.object(self.session, "_start_once", side_effect=setup):
            service.get("one", self.session.profile)
            worker = threading.Thread(target=start)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                service.errors["one"] = "previous failure"
                status = service.status("one", self.session.profile)
                self.assertTrue(status["available"])
                self.assertTrue(status["connecting"])
                self.assertEqual(status["proxy_state"], "not_checked")
                self.assertIs(service.get("one", self.session.profile), self.session)
                factory.assert_called_once()
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(errors)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.session.starting.is_set())
        with patch.object(service, "get", return_value=self.session):
            self.assertEqual(service.status("one", self.session.profile)["proxy_state"], "verified")

    def test_start_failure_clears_connecting_state(self):
        with patch.object(self.session, "_start_once", side_effect=CodexBlocked()), \
             patch("codex_service.time.sleep"):
            with self.assertRaises(CodexBlocked):
                ProfileSession.start(self.session)
        self.assertFalse(self.session.starting.is_set())

    def test_successful_connection_clears_old_status_error(self):
        service = CodexService(self.directory.name)
        service.errors["one"] = "temporary proxy failure"
        self.session.runtime.image_metadata = Mock()
        with patch.object(service, "get", return_value=self.session):
            status = service.status("one", self.session.profile)
        self.assertTrue(status["available"])
        self.assertEqual(status["proxy_state"], "verified")
        self.assertNotIn("one", service.errors)

    def test_first_connection_preserves_proxy_setup_failure_in_status(self):
        service = CodexService(self.directory.name)
        reason = "Не удалось подключиться через прокси"
        service.bootstrap = Mock(side_effect=ProxyUnavailable(reason))
        with patch("codex_proxy.effective_profile", side_effect=ProxyUnavailable()):
            result = service.action("one", self.session.profile, "retry")
            self.assertFalse(result["available"])
            self.assertEqual(result["reason"], reason)
            self.assertEqual(service.status("one", self.session.profile)["reason"], reason)

    def test_status_explains_invalid_proxy_before_a_session_exists(self):
        service = CodexService(self.directory.name)
        reason = "Для Codex нужен IP-адрес прокси"
        with patch("codex_proxy.effective_profile", side_effect=ProxyUnavailable(reason)):
            self.assertEqual(service.status("one", self.session.profile)["reason"], reason)

    def test_concurrent_same_chat_is_rejected_before_rpc(self):
        self.session.session_locks["chat-one"].acquire()
        with self.assertRaises(ProtocolError):
            self.session.run("chat-one", "prompt", "op-one", threading.Event())
        self.assertEqual(self.calls, [])
        self.session.session_locks["chat-one"].release()

    def test_older_completed_operation_cannot_replay_after_later_turn(self):
        first = self.session.run("chat-one", "first", "op-one", threading.Event())
        self.session.run("chat-one", "second", "op-two", threading.Event())
        count = len(self.calls)
        self.assertEqual(self.session.run("chat-one", "retry first", "op-one", threading.Event()), first)
        self.assertEqual(len(self.calls), count)

    def test_unacknowledged_interrupt_remains_uncertain(self):
        cancel = threading.Event(); cancel.set()
        def request(method, params=None, **kwargs):
            if method in ("config/read", "skills/list"):
                return {}
            if method == "thread/start":
                return {"thread": {"id": "thread-one"}}
            if method == "turn/start":
                return {"turn": {"id": "turn-one"}}
            raise ProtocolError("connection lost before interrupt acknowledgement")
        self.session.rpc = Mock(side_effect=request)
        self.session.close = Mock()
        with self.assertRaisesRegex(ProtocolError, "Исход хода неизвестен"):
            self.session.run("chat-one", "prompt", "op-one", cancel)
        self.session.close.assert_called_once()
        self.assertEqual(self.session.bindings.read("chat-one")["status"], "uncertain")
        with self.assertRaises(ProtocolError):
            self.session.run("chat-one", "retry", "op-one", threading.Event())

    def test_other_dialog_and_deleted_binding_stay_separate(self):
        self.session.run("chat-one", "prompt", "op-one", threading.Event())
        self.assertEqual(self.session.bindings.read("chat-two"), {})
        epoch = self.session.bindings.epoch("chat-one")
        self.session.forget("chat-one")
        self.session.bindings.write("chat-one", {"thread_id": "late"}, epoch)
        self.assertEqual(self.session.bindings.read("chat-one"), {})

    def test_search_route_refuses_network_tool_without_executing_it(self):
        self.session.active["thread-one"] = {"session_id": "chat-one", "cancel": threading.Event()}
        self.session.isolated_tool = Mock()
        result = self.session.tool_bridge({"kind": "tool_call", "request": {
            "session_id": "chat-one", "name": "web_search", "arguments": {"query": "example"}}})
        self.assertTrue(result["isError"])
        self.session.isolated_tool.assert_not_called()

    def test_url_reader_remains_available_with_native_codex_search(self):
        self.session.options["search"] = "codex"
        self.session.active["thread-one"] = {"session_id": "chat-one", "cancel": threading.Event(),
            "steps": [], "operation_id": "read-op"}
        self.session.isolated_tool = Mock(return_value={"text_content": "page", "route": "proxy"})
        result = self.session.tool_bridge({"kind": "tool_call", "request": {
            "session_id": "chat-one", "name": "fetch_webpage", "arguments": {"url": "https://public.test/page"}}})
        self.assertFalse(result["isError"])
        self.session.isolated_tool.assert_called_once()

    def test_research_uses_injected_codex_instead_of_paid_api(self):
        from research_controller import make_controller
        model = Mock(return_value='{"decision":"finish"}')
        with patch("research_controller.requests.post") as api:
            controller = make_controller({"engine": "codex", "api_key": "retained"}, [], model_call=model)
            self.assertEqual(controller("query", [], [], 5)["decision"], "finish")
            api.assert_not_called()
            model.assert_called_once()

    def test_redaction_hides_proxy_and_account_secrets(self):
        value = redact("private-pass https://user:pass@host/path Bearer SECRET sk-abcdefghijklmnop", ["private-pass"])
        self.assertNotIn("private-pass", value)
        self.assertNotIn("user:pass", value)
        self.assertNotIn("SECRET", value)
        self.assertNotIn("sk-abcdefghijklmnop", value)

    def test_note_changes_are_visible_and_not_executed_twice(self):
        steps = []
        self.session.local_tools = Mock(return_value={"ok": True})
        self.session.active["thread-one"] = {"session_id": "chat-one", "cancel": threading.Event(),
            "operation_id": "op-one", "on_step": steps.append, "steps": [], "refresh_notes": False}
        call = {"kind": "tool_call", "request": {"session_id": "chat-one", "name": "save_note",
                                                "arguments": {"title": "Visible note", "content": "once"}}}
        first = self.session.tool_bridge(call)
        self.assertFalse(first["isError"])
        self.assertEqual(first, self.session.tool_bridge(call))
        self.session.local_tools.assert_called_once()
        self.assertTrue(self.session.active["thread-one"]["refresh_notes"])
        self.assertTrue(any("Visible note" in event["text"] for event in self.session.events["chat-one"]))

    def test_windows_migration_quotes_previous_chat_without_replaying_actions(self):
        self.session.bindings.write("migrate-chat", {"thread_id": "old-linux-thread", "context": "old-context",
            "status": "completed", "operation_id": "old-operation", "result": {"reply": "old answer"}})
        self.session.history_reader = lambda _: [{"role": "user", "content": "Earlier request"},
            {"role": "assistant", "content": "The previous file was already created."}]
        result = self.session.run("migrate-chat", "Continue", "new-operation", threading.Event())
        self.assertEqual(result["reply"], "answer")
        turn = next(params for method, params in self.calls if method == "turn/start")
        text = turn["input"][1]["text"]
        self.assertIn("Earlier request", text)
        self.assertIn("do not repeat completed actions", text)
        self.assertTrue(text.endswith("Continue"))
        self.assertFalse(any(method == "thread/resume" for method, _ in self.calls))

    def test_updated_instructions_fork_saved_native_thread_with_complete_history(self):
        self.session.history_reader = Mock(return_value=[{"role": "assistant", "content": "Earlier answer"}])
        self.session.run("rules-chat", "first", "rules-one", threading.Event())
        self.calls.clear()
        self.session.profile["system_prompt"] = "Updated profile instructions"
        self.session.run("rules-chat", "next", "rules-two", threading.Event())
        self.assertTrue(any(method == "thread/fork" for method, _ in self.calls))
        self.assertFalse(any(method == "thread/start" for method, _ in self.calls))
        self.assertFalse(any(method == "thread/resume" for method, _ in self.calls))
        fork = next(params for method, params in self.calls if method == "thread/fork")
        self.assertEqual(fork["threadId"], "thread-one")
        self.assertIn("Updated profile instructions", fork["developerInstructions"])
        self.session.history_reader.assert_not_called()

    def test_relay_port_change_does_not_reset_native_thread(self):
        self.session.run("port-chat", "first", "port-one", threading.Event())
        self.calls.clear()
        self.session.runtime.tool_port = 5678
        self.session.run("port-chat", "next", "port-two", threading.Event())
        self.assertTrue(any(method == "thread/resume" for method, _ in self.calls))
        self.assertFalse(any(method == "thread/start" for method, _ in self.calls))

    def test_windows_tools_route_to_host_once_in_both_search_modes(self):
        self.session.local_tools=Mock(return_value={"ok":True,"platform":"Windows","path":"C:/Desktop/test.md"})
        self.session.isolated_tool=Mock(side_effect=AssertionError("Must run on host"))
        self.session.active["thread-one"]={"session_id":"host-chat","cancel":threading.Event(),"operation_id":"host-op",
            "on_step":None,"steps":[],"refresh_notes":False}
        for search in ("kid","codex"):
            self.session.options["search"]=search
            call={"kind":"tool_call","request":{"session_id":"host-chat","name":"windows_write_file",
                "arguments":{"file_name":search+".md","content":"real file","location":"desktop"}}}
            result=self.session.tool_bridge(call)
            self.assertFalse(result["isError"])
            self.assertEqual(self.session.tool_bridge(call),result)
        self.assertEqual(self.session.local_tools.call_count,2)
        self.session.isolated_tool.assert_not_called()


class JournalTests(unittest.TestCase):
    def test_pending_approval_disappears_when_turn_is_cancelled(self):
        queue=ApprovalQueue()
        cancel=threading.Event()
        result=[]
        worker=threading.Thread(target=lambda:result.append(queue.wait("p","s","item/commandExecution/requestApproval",{},cancel,str)))
        worker.start()
        import time
        deadline=time.monotonic()+2
        while not queue.pending() and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(len(queue.pending()),1)
        cancel.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(queue.pending(),[])
        self.assertEqual(result,[{"decision":"cancel"}])

    def test_github_api_and_archive_share_explicit_proxy_session_factory(self):
        import github_analyzer
        import requests
        session = Mock()
        session.get.side_effect = requests.ConnectionError("proxy unavailable")
        factory = Mock(return_value=session)
        with patch("github_analyzer.requests.Session") as default_session:
            result = github_analyzer.collect_metadata("octocat", "Hello-World", token="test", session_factory=factory)
        self.assertEqual(factory.call_count, 2)
        default_session.assert_not_called()
        self.assertTrue(result["errors"])
        self.assertFalse(session.trust_env)

    def test_completed_side_effect_is_shared_across_restarted_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            effect = Mock(return_value={"ok": True})
            first = ExecutionJournal(directory).execute("op", "save_note", {"text": "once"}, effect)
            second = ExecutionJournal(directory).execute("op", "save_note", {"text": "once"}, effect)
            self.assertEqual(first, second)
            effect.assert_called_once()

    def test_lost_acknowledgement_blocks_reexecution(self):
        with tempfile.TemporaryDirectory() as directory:
            effect = Mock(side_effect=RuntimeError("lost after side effect"))
            with self.assertRaises(RuntimeError):
                ExecutionJournal(directory).execute("op", "save_note", {}, effect)
            with self.assertRaises(ToolExecutionError):
                ExecutionJournal(directory).execute("op", "save_note", {}, effect)
            effect.assert_called_once()

    def test_approval_cancel_and_invalid_root_fail_closed(self):
        queue = ApprovalQueue()
        cancel = threading.Event(); cancel.set()
        self.assertEqual(queue.wait("p", "s", "item/fileChange/requestApproval", {}, cancel, str), {"decision": "cancel"})
        self.assertEqual(queue.pending(), [])
        self.assertEqual(queue.wait("p", "s", "item/fileChange/requestApproval", {"grantRoot": "/workspace-escape"}, cancel, str), {"decision": "decline"})


if __name__ == "__main__":
    unittest.main()
