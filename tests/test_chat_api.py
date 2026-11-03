"""API-turn contracts with temporary storage and no provider or embedding calls."""
import copy
import json
from unittest.mock import Mock, patch

from isolated_case import IsolatedStorageTestCase
from test_file_transfer import Request


class ApiChatTests(IsolatedStorageTestCase):
    def setUp(self):
        import desktop_app

        self.app = desktop_app
        self.config = {
            "active_profile": "main", "tool_send_mode": "all",
            "skip_search_on_smalltalk": False,
            "profiles": {"main": {"engine": "api", "system_prompt": "fixture rules",
                                   "base_url": "https://provider.invalid/v1", "model": "fixture"}},
        }
        self.memory = Mock()
        self.memory.build_memory_context.return_value = "\n[FACT-OLD]"
        self.memory.strip_memory_block.side_effect = lambda text: text.replace("\n[FACT-OLD]", "")
        self.calls = []

    def turn(self, responses=None, prompt="Describe my fixture preferences", **data):
        responses = iter(responses or [{"content": "fixture answer"}])

        def provider(**kwargs):
            self.calls.append(copy.deepcopy({key: value for key, value in kwargs.items()
                                             if key not in ("cancel_event", "on_thinking")}))
            response = next(responses)
            return response(**kwargs) if callable(response) else response

        request = Request("/api/chat", json.dumps({
            "prompt": prompt, "persona_id": "main", "session_id": "fixture", **data,
        }).encode("utf-8"))
        with patch.object(self.app, "load_config", side_effect=lambda: self.config), \
             patch.object(self.app, "save_config"), \
             patch.object(self.app, "user_profile_memory", self.memory), \
             patch.object(self.app, "get_live_balance", return_value=17), \
             patch.object(self.app.api_client, "call_chat_api", side_effect=provider):
            self.app.BridgeHandler._do_POST(request)
        self.assertEqual(request.status, 200, request.wfile.getvalue())
        self.assertFalse(self.app.LIVE_THINKING)
        self.assertFalse(self.app.LIVE_REASONING)
        self.assertFalse(self.app.CANCEL_EVENTS)
        return json.loads(request.wfile.getvalue())

    @staticmethod
    def tool(name, arguments):
        return {"content": "", "tool_calls": [{"function": {
            "name": name, "arguments": json.dumps(arguments),
        }}]}

    def test_automatic_memory_is_in_provider_context_and_persisted_history(self):
        payload = self.turn()
        self.memory.build_memory_context.assert_called_once_with("Describe my fixture preferences")
        self.assertIn("[FACT-OLD]", self.calls[0]["messages"][0]["content"])
        history = self.app.load_history()["fixture"]
        self.assertIn("[FACT-OLD]", history[0]["content"])
        self.assertEqual(history[-1]["content"], "fixture answer")
        self.assertEqual(payload["balance"], 17)

    def test_smalltalk_skips_retrieval_only_when_existing_flag_enabled(self):
        self.config["skip_search_on_smalltalk"] = True
        self.turn(prompt="hello")
        self.memory.build_memory_context.assert_not_called()
        self.config["skip_search_on_smalltalk"] = False
        self.turn(prompt="hello")
        self.memory.build_memory_context.assert_called_once_with("hello")

    def test_memory_failure_does_not_break_answer(self):
        self.memory.build_memory_context.side_effect = RuntimeError("fixture memory unavailable")
        payload = self.turn()
        self.assertEqual(payload["reply"], "fixture answer")
        self.assertNotIn("[FACT-OLD]", self.calls[0]["messages"][0]["content"])

    def test_remember_refreshes_memory_before_next_tool_round_and_repairs_missing_id(self):
        self.memory.build_memory_context.side_effect = ["\n[FACT-OLD]", "\n[FACT-NEW]"]
        self.memory.remember_user_fact.return_value = {"ok": True, "message": "saved"}
        self.turn([self.tool("remember_user_fact", {"fact": "fixture fact", "category": "work"}),
                   {"content": "remembered"}])
        self.memory.remember_user_fact.assert_called_once_with("fixture fact", "work")
        self.assertEqual(self.memory.build_memory_context.call_count, 2)
        self.assertIn("[FACT-NEW]", self.calls[1]["messages"][0]["content"])
        self.assertNotIn("[FACT-OLD]", self.calls[1]["messages"][0]["content"])
        history = self.app.sessions_memory["fixture"]
        assistant = next(m for m in history if m.get("tool_calls"))
        tool = next(m for m in history if m["role"] == "tool")
        self.assertEqual(assistant["tool_calls"][0]["id"], tool["tool_call_id"])

    def test_failed_remember_does_not_refresh_but_manage_memory_does(self):
        self.memory.remember_user_fact.return_value = {"ok": False, "message": "not saved"}
        self.turn([self.tool("remember_user_fact", {"fact": "fixture"}), {"content": "answer"}])
        self.assertEqual(self.memory.build_memory_context.call_count, 1)
        self.memory.reset_mock()
        self.memory.manage_memory.return_value = {"ok": False, "message": "not changed"}
        self.turn([self.tool("manage_memory", {"action": "update", "fact_id": "id",
                                               "fact": "new", "category": "work"}),
                   {"content": "answer"}])
        self.memory.manage_memory.assert_called_once_with("update", fact_id="id", fact="new", category="work")
        self.assertEqual(self.memory.build_memory_context.call_count, 2)

    def test_cancelled_stream_does_not_append_assistant_answer(self):
        def cancel(**kwargs):
            kwargs["cancel_event"].set()

        payload = self.turn([cancel])
        self.assertFalse(any(m["role"] == "assistant" for m in self.app.sessions_memory["fixture"]))
        self.assertTrue(payload["reply"])
        self.assertTrue(payload["cancelled"])

    def test_empty_answer_requests_text_without_tools_and_preserves_reasoning(self):
        def reasoning(**kwargs):
            kwargs["on_thinking"]("fixture reasoning")
            return {"content": ""}

        payload = self.turn([reasoning, {"content": "final text"}])
        self.assertIsNone(self.calls[1]["tools"])
        self.assertEqual(payload["reply"], "final text")
        self.assertEqual(payload["reasoning"], "fixture reasoning")
        self.assertTrue(any(m.get("reasoning") == "fixture reasoning" for m in self.calls[1]["messages"]))

    def test_provider_rejection_rolls_back_current_turn_and_saves_temporary_snapshot(self):
        def reject(**kwargs):
            raise RuntimeError("Content policy violation: fixture rejection")

        self.turn([reject])
        self.assertEqual([m["role"] for m in self.app.load_history()["fixture"]], ["system"])
        snapshots = list((self.app.USER_DATA_DIR / "history" / "_content_policy_debug").glob("fixture_*.json"))
        self.assertEqual(len(snapshots), 1)
        snapshot = json.loads(snapshots[0].read_text(encoding="utf-8"))
        self.assertEqual(snapshot["messages"][-1]["role"], "user")

    def test_deleted_session_is_not_persisted(self):
        self.app.DELETED_SESSIONS.add("fixture")
        self.turn()
        self.assertNotIn("fixture", self.app.sessions_memory)
        self.assertNotIn("fixture", self.app.load_sessions_meta())

    def test_studio_turn_preserves_separate_profile_history_and_word_tools(self):
        self.config["studio_active_profile"] = "studio"
        self.config["profiles"]["studio"] = {
            "engine": "api", "is_studio": True, "model": "studio-model",
            "system_prompt": "studio rules", "base_url": "https://studio.invalid/v1",
        }
        self.turn(studio=True, studio_persona_id="studio")
        self.assertEqual(self.calls[0]["model"], "studio-model")
        self.assertEqual(self.calls[0]["base_url"], "https://studio.invalid/v1")
        self.assertIn("[FACT-OLD]", self.calls[0]["messages"][0]["content"])
        self.assertEqual(self.calls[0]["tools"], self.app.api_client.WORD_TOOLS_SCHEMA)
        self.assertNotIn("fixture", self.app.sessions_memory)
        self.assertEqual(self.app.studio_sessions_memory["fixture"][-1]["content"], "fixture answer")
        self.assertNotIn("fixture", self.app.load_sessions_meta())
        self.assertEqual(self.app.studio_sessions_meta_memory["fixture"]["personaId"], "studio")
        self.assertEqual(self.config["studio_sessions_meta"]["fixture"]["personaId"], "studio")

    def test_legacy_data_url_preserves_multimodal_message_and_display(self):
        url = "data:image/png;base64,ZmFrZQ=="
        self.turn(attachments=[{"name": "fixture.png", "url": url}])
        user = next(m for m in self.calls[0]["messages"] if m["role"] == "user")
        self.assertEqual(user["content"][1], {"type": "image_url", "image_url": {"url": url}})
        persisted = next(m for m in self.app.load_history()["fixture"] if m["role"] == "user")
        self.assertEqual(persisted["display"], "Describe my fixture preferences")
        self.assertEqual(persisted["content"], user["content"])
