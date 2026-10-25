import json
import unittest
from unittest.mock import Mock, patch

from research_controller import make_controller, normalize_mode
from research_search import SearchBudget, investigate

URL = "https://example.com/answer"
PROFILE = {"api_key": "private-key", "base_url": "https://model.test/v1", "model": "same-model"}


def finish(**updates):
    return {"decision": "finish", "questions": [], "contradictions": [],
            "findings": [{"claim": "needle answer", "urls": [URL]}], "evidence_urls": [URL], **updates}


class ControllerTests(unittest.TestCase):
    def test_modes_keep_trajectory_or_start_fresh_with_memory(self):
        for mode, count in (("adaptive", 4), ("isolated", 2)):
            with self.subTest(mode=mode):
                controller = make_controller(PROFILE, ["forums"], mode, objective="Find the subscription model")
                response = Mock()
                response.json.return_value = {"choices": [{"message": {"content": json.dumps(finish())}}]}
                with patch("research_controller.requests.post", return_value=response) as post:
                    controller("needle", [{"url": URL, "body": "needle answer"}], [], 5)
                    controller("needle", [], [], 5)
                payload = post.call_args.kwargs["json"]
                self.assertEqual(len(payload["messages"]), count)
                self.assertEqual(payload["model"], "same-model")
                self.assertNotIn("tools", payload)
                self.assertNotIn("private-key", json.dumps(payload))
                card = json.loads(payload["messages"][-1]["content"])
                self.assertEqual(card["objective"], "Find the subscription model")
                self.assertRegex(card["current_date"], r"^\d{4}-\d{2}-\d{2}$")
                if mode == "isolated":
                    self.assertEqual(card["research_memory"]["findings"][0]["urls"], [URL])

    def test_memory_rejects_unknown_links_and_preserves_previous_findings(self):
        controller = make_controller(PROFILE, [], "isolated")
        responses = []
        for result in (finish(), finish(findings=[{"claim": "invented", "urls": ["https://unknown.test/"]}]), finish()):
            response = Mock()
            response.json.return_value = {"choices": [{"message": {"content": json.dumps(result)}}]}
            responses.append(response)
        with patch("research_controller.requests.post", side_effect=responses) as post:
            controller("needle", [{"url": URL}], [], 5)
            controller("needle", [], [], 5)
            controller("needle", [], [], 5)
        memory = json.loads(post.call_args.kwargs["json"]["messages"][-1]["content"])["research_memory"]
        self.assertEqual(memory["findings"], [{"claim": "needle answer", "urls": [URL]}])

    def test_invalid_json_is_error_not_finish(self):
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": "not json"}}]}
        with patch("research_controller.requests.post", return_value=response):
            with self.assertRaises(ValueError):
                make_controller(PROFILE, [])("needle", [], [], 5)

    def test_truncated_model_output_is_rejected_even_if_json_parses(self):
        response = Mock()
        response.json.return_value = {"choices": [{"finish_reason": "length", "message": {"content": json.dumps(finish())}}]}
        with patch("research_controller.requests.post", return_value=response):
            with self.assertRaisesRegex(ValueError, "truncated"):
                make_controller(PROFILE, [])("needle", [], [], 5)

    def test_default_mode_and_missing_profile(self):
        self.assertEqual(normalize_mode(None), "adaptive")
        self.assertEqual(normalize_mode("unexpected"), "adaptive")
        self.assertIsNone(make_controller({}, []))

    def run_research(self, plan, **options):
        defaults = {"search": lambda *a: {"status": "ok", "items": [{"url": URL, "title": "needle", "body": "needle answer"}]},
                    "read": lambda u: {"text_content": "needle direct answer"}, "providers": ("test",),
                    "budget": SearchBudget(max_pages=1, max_rounds=3, max_planner_calls=2)}
        defaults.update(options)
        return investigate("needle", planner=plan, **defaults)

    def test_supported_answer_stops_after_first_batch(self):
        plan = Mock(return_value=finish())
        result = self.run_research(plan)
        self.assertEqual(result.report["stop_reason"], "sufficient_evidence")
        self.assertEqual(result.report["requests"], 1)
        self.assertEqual(plan.call_count, 1)

    def test_snippets_metadata_conflicts_gaps_and_missing_claim_cannot_finish(self):
        cases = [({"max_reads": 0}, {}, finish()),
                 ({}, {"content_kind": "video_description"}, finish()),
                 ({}, {}, finish(questions=["Which model?"])),
                 ({}, {}, finish(contradictions=["Different model names"])),
                 ({}, {}, finish(findings=[])),
                 ({}, {}, {"decision": "finish", "evidence_urls": [URL]})]
        for budget, data, decision in cases:
            with self.subTest(decision=decision, budget=budget, data=data):
                result = self.run_research(Mock(return_value=decision),
                    read=lambda u: {"text_content": "needle direct answer", **data},
                    budget=SearchBudget(max_pages=1, max_rounds=1, **budget))
                self.assertNotEqual(result.report["stop_reason"], "sufficient_evidence")
                self.assertTrue(result.report["assessments"][0]["finish_rejected"])

    def test_gap_drives_query_on_selected_platform_then_finishes(self):
        calls = []
        def search(provider, query, *args):
            calls.append((provider, query))
            return {"status": "ok", "items": [{"url": URL, "title": "needle"}]}
        plan = Mock(side_effect=[{"decision": "search", "questions": ["model name"],
                                  "actions": [{"source": "forums", "query": "needle subscription model"}]}, finish()])
        result = self.run_research(plan, search=search, routing={"primary": "test", "catalog": ["forums"], "initial": []})
        self.assertIn(("test:forums", "needle subscription model"), calls)
        self.assertEqual(result.report["stop_reason"], "sufficient_evidence")

    def test_requested_read_prioritized_in_next_batch(self):
        urls = [f"https://example.com/{i}" for i in range(6)]
        reads = []
        def read(url):
            reads.append(url)
            return {"text_content": "needle "+url}
        plan = Mock(return_value={"decision": "read", "read_urls": [urls[-1]], "questions": ["details"]})
        self.run_research(plan, read=read,
            search=lambda *a: {"status": "ok", "items": [{"url": u, "title": "needle"} for u in urls]})
        self.assertEqual(reads[3], urls[-1])

    def test_model_cannot_raise_budget_or_call_arbitrary_provider(self):
        plan = Mock(return_value={"decision": "search", "max_requests": 999,
                                  "actions": [{"query": "needle", "source": "unlisted"}]})
        result = self.run_research(plan, budget=SearchBudget(max_requests=1, max_reads=1, max_planner_calls=1))
        self.assertEqual(result.report["requests"], 1)
        self.assertLessEqual(result.report["reads"], 1)
        self.assertEqual(plan.call_count, 1)

    def test_read_action_defers_queued_pagination(self):
        events = []
        urls = [f"https://example.com/{i}" for i in range(6)]
        def search(*args):
            events.append("search")
            return {"status": "ok", "items": [{"url": u, "title": "needle"} for u in urls]}
        def read(url):
            events.append(url)
            return {"text_content": "needle "+url}
        self.run_research(Mock(return_value={"decision": "read", "read_urls": [urls[-1]]}),
            search=search, read=read, budget=SearchBudget(max_pages=3, max_rounds=2))
        self.assertEqual(events.count("search"), 1)
        self.assertEqual(events[4], urls[-1])

    def test_finish_preserves_supporting_source_in_short_result(self):
        other = "https://example.com/other"
        result = self.run_research(Mock(return_value=finish()), max_results=1,
            search=lambda *a: {"status": "ok", "items": [{"url": other, "title": "needle needle needle"},
                                                        {"url": URL, "title": "needle"}]},
            read=lambda u: {"text_content": "needle "+u})
        self.assertEqual(result.report["stop_reason"], "sufficient_evidence")
        self.assertEqual(result[0]["url"], URL)


if __name__ == "__main__":
    unittest.main()
