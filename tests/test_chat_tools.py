import threading
import time
import unittest
from unittest.mock import Mock, patch

from isolated_case import IsolatedStorageTestCase


class ApiToolTests(IsolatedStorageTestCase):
    def setUp(self):
        from application import chat_tools

        self.tools = chat_tools

    def test_web_search_preserves_planner_sources_progress_and_error_contract(self):
        cancel = threading.Event()
        steps, profile = [], {"search_results": 7, "search_region": "fixture-region"}
        args = {"query": "fixture", "objective": "independent task", "sources": ["youtube"]}
        with patch.object(self.tools.search_tools, "web_search", return_value=([], "fixture outage")) as search, \
             patch.object(self.tools.search_tools, "make_research_planner", return_value="planner") as planner, \
             patch.object(self.tools.retrieval_quality, "record_request"):
            output = self.tools.web_search(args, steps, time.perf_counter(), profile, cancel,
                                           lambda: {"search_research_mode": "isolated"}, Mock())
        self.assertIn("fixture outage", output)
        self.assertEqual(len(steps), 1)
        self.assertEqual(search.call_args.kwargs["max_results"], 7)
        self.assertEqual(search.call_args.kwargs["sources"], ["youtube"])
        self.assertIs(search.call_args.kwargs["cancel_event"], cancel)
        self.assertEqual(search.call_args.kwargs["planner"], "planner")
        planner.assert_called_once_with(profile, mode="isolated", objective="independent task")

    def test_saved_note_has_existing_limits_and_updates_both_views_once(self):
        store, log = Mock(), Mock()
        steps, emitted = [], []
        self.tools.save_note({"title": "t" * 130, "content": "c" * 4100}, steps, emitted, store, log)
        store.assert_called_once()
        note = store.call_args.args[0]
        self.assertEqual(set(note), {"id", "title", "content", "createdAt", "author"})
        self.assertEqual(note["author"], "assistant")
        self.assertEqual(len(note["title"]), 120)
        self.assertTrue(note["content"].startswith("c" * 4000 + "\n"))
        self.assertEqual(emitted, [{"title": note["title"], "content": note["content"]}])
        self.assertEqual(len(steps), 1)
        store.reset_mock()
        self.tools.save_note({"content": " "}, [], [], store, log)
        store.assert_not_called()

    def test_profile_prompt_updates_same_config_and_empty_prompt_does_not_save(self):
        profile = {}
        cfg = {"profiles": {"fixture": profile}}
        save = Mock()
        steps = []
        self.tools.set_system_prompt({"prompt": "x" * 8100}, steps, profile, cfg, save, Mock())
        self.assertTrue(profile["system_prompt"].startswith("x" * 8000 + "\n"))
        self.assertIs(save.call_args.args[0]["profiles"]["fixture"], profile)
        self.assertEqual(len(steps), 1)
        save.reset_mock()
        self.tools.set_system_prompt({"prompt": " "}, [], profile, cfg, save, Mock())
        save.assert_not_called()

    def test_cancelled_file_creation_never_runs_executor(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(self.tools.file_tools, "create_downloadable_file") as create:
            output = self.tools.create_file("create_downloadable_file", {"file_name": "fixture.txt"}, [], cancel)
        create.assert_not_called()
        self.assertIsInstance(output, str)
        self.assertTrue(output)
