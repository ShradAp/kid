"""History windows must preserve the current question and provider contracts."""
import copy
import os
import unittest
from unittest.mock import patch

from application.chat_messages import _prepare_messages_for_api, _strip_reasoning_from_reply


class ChatMessageTests(unittest.TestCase):
    def test_history_window_keeps_complete_current_tool_turn(self):
        messages = [{"role": "system", "content": "system", "private": "hidden"},
                    {"role": "user", "content": "old"},
                    {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": "current", "display": "UI only"}]
        for number in range(4):
            messages.extend([
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": str(number), "function": {"name": "local_search", "arguments": "{}"}}]},
                {"role": "tool", "content": "evidence", "tool_call_id": str(number)},
            ])
        prepared = _prepare_messages_for_api(messages, None, max_msgs=2)
        self.assertEqual(prepared[0], {"role": "system", "content": "system"})
        self.assertEqual(prepared[1], {"role": "user", "content": "current"})
        self.assertEqual(len(prepared), 10)
        self.assertEqual([m["tool_call_id"] for m in prepared if m["role"] == "tool"],
                         ["0", "1", "2", "3"])

    def test_unlimited_history_still_drops_orphan_and_incomplete_tool_prefix(self):
        messages = [{"role": "tool", "content": "orphan", "tool_call_id": "old"},
                    {"role": "assistant", "content": "", "tool_calls": [{"id": "missing"}]},
                    {"role": "tool", "content": "wrong result", "tool_call_id": "other"},
                    {"role": "user", "content": "question", "attachments": [{"name": "private"}]}]
        self.assertEqual(_prepare_messages_for_api(messages, None),
                         [{"role": "user", "content": "question"}])

    def test_only_latest_multimodal_message_keeps_images_without_mutating_parts(self):
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
        messages = [{"role": "system", "content": "system"},
                    {"role": "user", "content": [image]},
                    {"role": "assistant", "content": "seen"},
                    {"role": "user", "content": [{"type": "text", "text": "attachment"}, image]}]
        original = copy.deepcopy(messages)
        prepared = _prepare_messages_for_api(messages, None)
        self.assertEqual(prepared[1]["content"][0]["type"], "text")
        self.assertEqual(prepared[-1]["content"][-1], image)
        self.assertEqual(messages, original)

    def test_environment_caps_text_and_total_window_keeps_newest_message(self):
        with patch.dict(os.environ, {"CHAT_MSG_MAX_CHARS": "4", "CHAT_MSG_TEXT_PART_MAX_CHARS": "3"}):
            prepared = _prepare_messages_for_api([
                {"role": "user", "content": "123456"},
                {"role": "assistant", "content": "response"},
                {"role": "user", "content": [{"type": "text", "text": "abcdef"}]},
            ], None)
        self.assertTrue(prepared[0]["content"].startswith("1234\n"))
        self.assertTrue(prepared[-1]["content"][0]["text"].startswith("abc\n"))
        self.assertEqual(_prepare_messages_for_api([
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "a" * 30},
            {"role": "user", "content": "new"},
        ], max_chars=10), [{"role": "user", "content": "new"}])

    def test_reasoning_prefix_handles_whitespace_and_keeps_short_matches(self):
        reasoning = "long reasoning sentence " * 4
        self.assertEqual(_strip_reasoning_from_reply(reasoning + "Answer", reasoning), "Answer")
        self.assertEqual(_strip_reasoning_from_reply(reasoning, reasoning), "")
        self.assertEqual(_strip_reasoning_from_reply(reasoning.replace(" ", "\n") + "Answer", reasoning), "Answer")
        self.assertEqual(_strip_reasoning_from_reply("short answer", "short"), "short answer")
        self.assertEqual(_strip_reasoning_from_reply("different", reasoning), "different")
