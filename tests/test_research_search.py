"""Research regressions with deterministic providers; no real network or keys."""
import json
import ast
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from research_search import SearchBudget, SearchResults, canonical_url, investigate, relevant_fragment
import search_tools


def source(url, body="needle evidence", title="needle"):
    return {"url": url, "body": body, "title": title}


def batch(*items):
    return {"items": list(items), "status": "ok" if items else "empty"}


class ResearchTests(unittest.TestCase):
    def run_search(self, search, **kwargs):
        options = {"read": lambda url: {"text_content": "needle evidence"}, "providers": ("test",)}
        options.update(kwargs)
        return investigate("needle", search=search, **options)

    def test_finds_source_on_third_page(self):
        calls = []
        def search(provider, q, page, count, timeout):
            calls.append(page)
            return batch(source(f"https://example.com/{page}", title="needle " + ("answer" if page == 3 else "")))
        results = self.run_search(search)
        self.assertEqual(calls, [1, 2, 3])
        self.assertIn("https://example.com/3", [s["url"] for s in results.report["sources"]])
        deep = next(s for s in results.report["sources"] if s["url"].endswith("/3"))
        self.assertEqual(deep["discovered_by"][0]["page"], 3)

    def test_evidence_creates_new_query(self):
        calls = []
        def search(provider, q, page, count, timeout):
            calls.append(q)
            if q == "needle":
                return batch(source("https://example.com/a", title='needle «Hidden Model»'))
            return batch(source("https://other.com/answer"))
        results = self.run_search(search, budget=SearchBudget(max_pages=1),
                                  read=lambda url: {"text_content": "needle evidence " + url})
        self.assertTrue(any("Hidden Model" in q for q in calls))
        self.assertTrue(any("other.com" in s["url"] for s in results))

    def test_both_providers_and_provenance(self):
        results = self.run_search(lambda *a: batch(source("https://example.com/?utm_source=x")),
                                  providers=("first", "second"), budget=SearchBudget(max_pages=1))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0]["discovered_by"]), 2)

    def test_repeated_page_ends_branch(self):
        calls = []
        def search(*args):
            calls.append(args)
            return batch(source("https://example.com/a"))
        self.run_search(search)
        self.assertEqual(len(calls), 2)

    def test_provider_overlap_does_not_hide_second_page(self):
        def search(provider, q, page, *args):
            if page == 1:
                return batch(source("https://shared.com/a"))
            if provider == "second" and page == 2:
                return batch(source("https://hidden.com/answer"))
            return batch()
        results = self.run_search(search, providers=("first", "second"),
                                  read=lambda url: {"text_content": "needle " + url})
        self.assertTrue(any(s["url"] == "https://hidden.com/answer" for s in results))

    def test_request_and_read_limits(self):
        search = Mock(side_effect=lambda *a: batch(*(source(f"https://example.com/{i}") for i in range(10))))
        read = Mock(return_value={"text_content": "needle"})
        results = self.run_search(search, read=read, budget=SearchBudget(max_requests=1, max_reads=2))
        self.assertEqual(search.call_count, 1)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(results.report["stop_reason"], "request_budget")

    def test_candidate_limit(self):
        results = self.run_search(lambda *a: batch(*(source(f"https://example.com/{i}") for i in range(20))),
                                  budget=SearchBudget(max_candidates=3))
        self.assertEqual(results.report["candidates"], 3)
        self.assertEqual(results.report["stop_reason"], "candidate_budget")

    def test_unconfigured_provider_is_not_retried(self):
        calls = []
        def search(provider, q, page, *args):
            calls.append(provider)
            if provider == "missing":
                return {"status": "unconfigured", "items": []}
            return batch(source(f"https://example.com/{page}"))
        results = self.run_search(search, providers=("missing", "working"))
        self.assertEqual(calls.count("missing"), 1)
        self.assertTrue(results)
        self.assertEqual(results.report["errors"][0]["status"], "unconfigured")

    def test_failures_are_not_reported_as_empty_search(self):
        results = self.run_search(lambda *args: {"items": [], "status": "error", "error": "timeout"})
        self.assertFalse(results)
        self.assertEqual(results.report["errors"][0]["error"], "timeout")

    def test_cancel_before_search(self):
        event = threading.Event()
        event.set()
        search = Mock()
        results = self.run_search(search, cancel_event=event)
        search.assert_not_called()
        self.assertEqual(results.report["stop_reason"], "cancelled")

    def test_cancel_during_read_returns_without_waiting(self):
        event, entered, release = threading.Event(), threading.Event(), threading.Event()
        def read(url):
            entered.set()
            release.wait(2)
            return {"text_content": "needle late"}
        def cancel():
            entered.wait(1)
            event.set()
        worker = threading.Thread(target=cancel)
        worker.start()
        started = time.monotonic()
        try:
            results = self.run_search(lambda *a: batch(source("https://example.com/a")), read=read, cancel_event=event)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(results.report["stop_reason"], "cancelled")
            self.assertNotEqual(results[0]["read_status"], "read")
        finally:
            release.set()
            worker.join()

    def test_deadline_during_search(self):
        release = threading.Event()
        def search(*args):
            release.wait(1)
            return batch()
        try:
            results = self.run_search(search, budget=SearchBudget(max_seconds=.05))
            self.assertEqual(results.report["stop_reason"], "time_budget")
            self.assertLess(results.report["elapsed_seconds"], .5)
        finally:
            release.set()

    def test_copies_not_counted_as_independent_evidence(self):
        results = self.run_search(lambda *a: batch(source("https://one.com/a"), source("https://two.com/b")),
                                  budget=SearchBudget(max_pages=1))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(results.report["sources"]), 2)
        self.assertTrue(any(s["duplicate_of"] for s in results.report["sources"]))

    def test_unavailable_source_retains_snippet(self):
        results = self.run_search(lambda *a: batch(source("https://example.com/a")),
                                  read=lambda url: {"skipped": True, "error": "blocked"})
        self.assertEqual(results[0]["body"], "needle evidence")
        self.assertEqual(results[0]["read_status"], "unavailable")
        self.assertIn("только сниппет", search_tools.format_search_block(results))

    def test_model_plans_are_bounded_and_failures_fall_back(self):
        planner = Mock(side_effect=ValueError("malformed model output"))
        results = self.run_search(lambda *a: batch(source("https://example.com/a", title='needle «Hidden Model»')),
                                  planner=planner, budget=SearchBudget(max_planner_calls=1))
        self.assertEqual(planner.call_count, 1)
        self.assertTrue(any('Hidden Model' in q["query"] for q in results.report["queries"]))
        self.assertTrue(any(e.get("provider") == "planner" for e in results.report["errors"]))

    def test_model_queries_and_questions(self):
        planner = Mock(return_value={"queries": ["needle exact model", 42], "questions": ["Which model?"]})
        results = self.run_search(lambda *a: batch(source("https://example.com/a")), planner=planner,
                                  budget=SearchBudget(max_pages=1, max_planner_calls=1))
        self.assertTrue(any(q["query"] == "needle exact model" for q in results.report["queries"]))
        self.assertEqual(results.report["questions"], ["Which model?"])

    def test_semantic_url_parameters_are_preserved(self):
        self.assertEqual(canonical_url("https://EXAMPLE.com:443/watch?v=abc&utm_source=x#t=3"),
                         "https://example.com/watch?v=abc")
        self.assertNotEqual(canonical_url("https://example.com/watch?v=a"), canonical_url("https://example.com/watch?v=b"))
        self.assertEqual(canonical_url("file:///private"), "")
        self.assertEqual(canonical_url("https://user:password@example.com/"), "")

    def test_fragment_finds_mentions_at_end(self):
        self.assertIn("needle answer", relevant_fragment("irrelevant " * 2000 + "needle answer", "needle", 800))

    def test_invalid_budgets(self):
        for kwargs in ({"max_reads": -1}, {"max_requests": 1.5}, {"max_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                SearchBudget(**kwargs)


class AdapterTests(unittest.TestCase):
    def test_legacy_core_entry_uses_shared_search(self):
        # Extract the method to avoid constructing the module-global browser.
        tree = ast.parse((Path(__file__).resolve().parents[1] / "network_agent" / "core.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NetworkAgentCore")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "search_and_summarize")
        namespace = {}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "core-method", "exec"), namespace)
        core = SimpleNamespace(searxng_url="http://127.0.0.1:9999", _get_page_data=Mock())
        with patch.object(search_tools, "web_search", return_value=([], "expected error")) as search:
            result = namespace["search_and_summarize"](core, "needle", deep_scrape_top=3)
        self.assertEqual(result, "expected error")
        self.assertEqual(search.call_args.kwargs["searxng_url"], core.searxng_url)
        self.assertEqual(search.call_args.kwargs["budget"].max_reads, 3)

    def test_searxng_pagination(self):
        response = Mock()
        response.json.return_value = {"results": [{"title": "x", "url": "https://example.com/"}]}
        with patch.object(search_tools.requests, "get", return_value=response) as get:
            result = search_tools._searxng_search("needle", 10, "ru", page=3, detailed=True)
        self.assertEqual(get.call_args.kwargs["params"]["pageno"], 3)
        self.assertEqual(result["status"], "ok")

    def test_serper_pagination(self):
        response = Mock()
        response.json.return_value = {"organic": [{"title": "x", "link": "https://example.com/"}]}
        with patch.object(search_tools, "load_config", return_value={"search_api_key": "test"}), \
                patch.object(search_tools.requests, "post", return_value=response) as post:
            result = search_tools._serper_search("needle", 10, "ru", page=4, detailed=True)
        self.assertEqual(post.call_args.kwargs["json"]["page"], 4)
        self.assertEqual(result["status"], "ok")

    def test_public_interface_stays_tuple_with_list(self):
        with patch.object(search_tools, "_searxng_search", return_value=batch(source("https://example.com/a"))):
            results, error = search_tools.web_search("needle", backend="searxng", reader=lambda url: {},
                                                     budget=SearchBudget(max_requests=1, max_reads=0))
        self.assertIsInstance(results, list)
        self.assertIsNone(error)

    def test_planner_has_separate_context_no_tools_or_secret_in_messages(self):
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": json.dumps({"queries": []})}}]}
        planner = search_tools.make_research_planner({"api_key": "secret", "base_url": "https://example.com/v1", "model": "test"})
        with patch.object(search_tools.requests, "post", return_value=response) as post:
            self.assertEqual(planner("needle", [], [], 5), {"queries": []})
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("tools", payload)
        self.assertNotIn("secret", json.dumps(payload))
        self.assertEqual(len(payload["messages"]), 2)


if __name__ == "__main__":
    unittest.main()
