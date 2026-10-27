"""Turn recovery, tool idempotency and profile isolation without an account."""
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from codex_engine import LimitsCache, ProtocolError
from codex_service import ApprovalQueue, ProfileSession, SessionBindings, redact
from tool_execution import ExecutionJournal, ToolExecutionError


class SessionTests(unittest.TestCase):
    def setUp(self):
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
            if method in ("thread/start", "thread/resume"):
                return {"thread": {"id": "thread-one"}}
            if method == "turn/start":
                if self.fail:
                    raise ProtocolError("connection lost")
                self.session.notify("item/agentMessage/delta", {"threadId": params["threadId"], "delta": "answer"})
                self.session.notify("turn/completed", {"threadId": params["threadId"], "turn": {"id": "turn-one", "status": "completed"}})
                return {"turn": {"id": "turn-one"}}
            if method == "thread/read":
                return {"thread": {"turns": [{"id": "turn-one", "status": "completed", "items": [
                    {"id": "op-one", "type": "userMessage"}, {"type": "agentMessage", "text": "recovered answer"}]}]}}
            return {}
        self.session.rpc = Mock(side_effect=request)

    def test_stream_and_completed_request_are_not_replayed(self):
        texts = []
        result = self.session.run("chat-one", "prompt", "op-one", threading.Event(), on_text=texts.append)
        self.assertEqual(result["reply"], "answer")
        self.assertEqual(texts, ["answer"])
        count = len(self.calls)
        self.assertEqual(self.session.run("chat-one", "prompt", "op-one", threading.Event()), result)
        self.assertEqual(len(self.calls), count)

    def test_commentary_and_final_items_do_not_duplicate_the_answer(self):
        def request(method, params=None, **kwargs):
            if method == "thread/start": return {"thread":{"id":"phase-thread"}}
            if method == "turn/start":
                for item_id, phase, text in (("progress","commentary","Создаю документ"),("final","final_answer","Документ создан.")):
                    self.session.notify("item/started", {"threadId":"phase-thread","item":{"id":item_id,"type":"agentMessage","phase":phase}})
                    self.session.notify("item/agentMessage/delta", {"threadId":"phase-thread","itemId":item_id,"delta":text})
                    self.session.notify("item/completed", {"threadId":"phase-thread","item":{"id":item_id,"type":"agentMessage","phase":phase,"text":text}})
                self.session.notify("turn/completed", {"threadId":"phase-thread","turn":{"status":"completed"}})
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
        with self.assertRaises(ProtocolError):
            self.session.run("chat-one", "prompt", "op-two", threading.Event())
        self.assertEqual(sum(method == "turn/start" for method, _ in self.calls), 1)
        recovered = self.session.recover("chat-one")
        self.assertEqual(recovered["reply"], "recovered answer")
        self.assertEqual(sum(method == "turn/start" for method, _ in self.calls), 1)
        resume = next(params for method, params in self.calls if method == "thread/resume")
        self.assertIn("1234", resume["config"]["mcp_servers"]["kid"]["args"])

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
            "session_id": "chat-one", "name": "fetch_webpage", "arguments": {"url": "https://example.com"}}})
        self.assertTrue(result["isError"])
        self.session.isolated_tool.assert_not_called()

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
        text = turn["input"][0]["text"]
        self.assertIn("Earlier request", text)
        self.assertIn("do not repeat completed actions", text)
        self.assertTrue(text.endswith("Continue"))
        self.assertFalse(any(method == "thread/resume" for method, _ in self.calls))

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
