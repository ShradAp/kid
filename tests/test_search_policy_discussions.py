"""Topic routing, entity relevance and bounded evidence from thread replies."""
import unittest
from unittest.mock import MagicMock, Mock, patch

import discussion_reader as d
import public_sources as p
import search_tools
from search_policy import default_scopes, validate_scopes
from research_search import SearchBudget, investigate, relevant_fragment, _rank, entity_terms


class RoutingTests(unittest.TestCase):
    def test_five_initial_checkpoints_including_open_web(self):
        names = default_scopes("Kakadu AI subscription model")
        self.assertLessEqual(len(names), 4)
        self.assertNotIn("pikabu", names)
        self.assertNotIn("vk", names)

    def test_platform_clues_are_preserved(self):
        self.assertEqual(default_scopes("TikTok Kakadu AI")[0], "tiktok")
        self.assertIn("technical", default_scopes("Python API error"))
        self.assertNotIn("tiktok", default_scopes("Python API error"))
        self.assertEqual(default_scopes("news today"), ["articles", "telegram"])

    def test_pikabu_is_optional_or_explicit(self):
        self.assertIn("pikabu", default_scopes("Пикабу отзывы"))
        self.assertNotIn("pikabu.ru", p.SEARCH_DOMAINS["articles"])

    def test_invalid_model_scope_names_are_not_executed(self):
        self.assertIsNone(validate_scopes(["https://localhost/", "crawl_author"], p.SEARCH_DOMAINS))
        self.assertEqual(validate_scopes(["forums", "forums", "technical"], p.SEARCH_DOMAINS), ["forums", "technical"])

    def test_explicit_empty_sources_only_searches_web(self):
        with patch.object(search_tools, "_searxng_search", return_value={"items": [], "status": "empty"}) as search:
            result, _ = search_tools.web_search("needle", backend="searxng", sources=[],
                                                budget=SearchBudget(max_reads=0, max_rounds=1))
        self.assertEqual(search.call_count, 1)
        self.assertEqual(result.report["routing"]["initial"], [])

    def test_planner_can_add_only_two_allowed_directions(self):
        routing = {"primary": "test", "catalog": list(p.SEARCH_DOMAINS), "initial": ["articles"], "origin": "test"}
        plan = Mock(return_value={"queries": [], "sources": ["tiktok", "pikabu", "vk", "crawl_author"]})
        def search(provider, query, page, *args):
            return {"status": "ok", "items": [{"url": f"https://example.com/{provider}/{page}", "title": "needle"}]}
        result = investigate("needle", search=search, read=Mock(), providers=("test",), planner=plan,
                             routing=routing, budget=SearchBudget(max_requests=8, max_reads=0, max_rounds=2))
        self.assertEqual(result.report["routing"]["optional"], ["tiktok", "pikabu"])
        self.assertTrue(any(q["provider"] == "test:pikabu" for q in result.report["queries"]))
        self.assertLessEqual(result.report["requests"], 8)

    def test_explicit_web_only_cannot_be_expanded_by_planner(self):
        routing = {"primary": "test", "catalog": list(p.SEARCH_DOMAINS), "initial": [], "locked": True}
        result = investigate("needle", search=lambda *a: {"items": [{"url": "https://example.com/", "title": "needle"}], "status": "ok"},
                             read=Mock(), providers=("test",), routing=routing,
                             planner=lambda *a: {"sources": ["tiktok"]}, budget=SearchBudget(max_reads=0))
        self.assertEqual(result.report["routing"]["optional"], [])


class RelevanceTests(unittest.TestCase):
    def test_brand_outweighs_generic_subscription_seo(self):
        query = 'Kakadu AI subscription model'
        good = {"title": "Какаду AI", "body": "Информация о подписке", "url": "https://example.com/"}
        noise = {"title": "Best AI subscription model", "body": "AI subscription model "*50, "url": "https://noise.com/"}
        self.assertGreater(_rank(good, query), _rank(noise, query))
        self.assertLess(_rank(noise, query)[0], 0)

    def test_generic_ai_in_quotes_is_not_a_brand_anchor(self):
        anchors = entity_terms('"Kakadu AI" model')
        self.assertIn("kakadu", anchors)
        self.assertNotIn("ai", anchors)

    def test_generic_news_query_has_no_fake_entity(self):
        self.assertFalse(entity_terms("latest news today"))

    def test_rare_mention_at_end_survives_repetition_at_start(self):
        text = "needle "*3000 + "RareModel decisive answer"
        fragment = relevant_fragment(text, "needle RareModel", limit=1200)
        self.assertIn("RareModel decisive answer", fragment)
        self.assertLessEqual(len(fragment), 1200)

    def test_irrelevant_diverse_domains_cannot_displace_useful_same_host(self):
        items = [{"url": "https://official.test/one", "title": "Kakadu AI subscription model"},
                 {"url": "https://official.test/two", "title": "Kakadu model"},
                 {"url": "https://other.test/", "title": "Best subscription model"}]
        result = investigate("Kakadu AI subscription model", providers=("test",),
                             search=lambda *a: {"items": items, "status": "ok"}, read=Mock(),
                             budget=SearchBudget(max_requests=1, max_reads=0), max_results=2)
        self.assertEqual([x["url"] for x in result], ["https://official.test/one", "https://official.test/two"])
        self.assertEqual(result.report["sources"][2]["relevance"], "weak")


class DiscussionTests(unittest.TestCase):
    def session(self, values):
        session = MagicMock()
        session.__enter__.return_value = session
        responses = []
        for value in values:
            response = Mock()
            response.json.return_value = value
            responses.append(response)
        session.get.side_effect = responses
        session.diagnostics.return_value = {"requests": len(responses)}
        return session

    def test_github_reads_first_and_latest_page_under_three_requests(self):
        root = {"title": "needle", "body": "root", "comments": 31}
        first = [{"id": i, "body": "old"} for i in range(1,16)]
        latest = [{"id": 31, "body": "decisive answer"}]
        session = self.session([root, first, latest])
        with patch.object(d, "GuestSession", return_value=session):
            result = d.read_discussion("https://github.com/org/repo/issues/1")
        self.assertEqual(session.get.call_count, 3)
        self.assertIn("page=3", session.get.call_args.args[0])
        self.assertTrue(result["thread_truncated"])
        self.assertEqual(result["comment_evidence"][-1]["url"], "https://github.com/org/repo/issues/1#issuecomment-31")
        self.assertIn("decisive answer", result["text_content"])

    def test_comment_page_error_preserves_root_and_partial_status(self):
        session = self.session([{"title": "needle", "body": "root", "comments": 10}])
        with patch.object(d, "GuestSession", return_value=session):
            result = d.read_discussion("https://github.com/org/repo/issues/1")
        self.assertIn("root", result["text_content"])
        self.assertTrue(result["thread_truncated"])
        self.assertNotEqual(result["discussion_status"], "read")

    def test_reddit_nested_replies_have_separate_links_and_more_is_partial(self):
        child = {"kind": "t1", "data": {"id": "c2", "body": "nested answer"}}
        value = [{"data": {"children": [{"data": {"title": "needle", "selftext": "root", "num_comments": 5}}]}},
                 {"data": {"children": [{"kind": "t1", "data": {"id": "c1", "body": "reply", "replies": {"data": {"children": [child]}}}}, {"kind": "more"}]}}]
        session = self.session([value])
        with patch.object(d, "GuestSession", return_value=session):
            result = d.read_discussion("https://reddit.com/r/test/comments/abc/slug/")
        self.assertEqual(len(result["comment_evidence"]), 2)
        self.assertTrue(result["thread_truncated"])
        self.assertTrue(result["comment_evidence"][1]["url"].endswith("/c2/"))

    def test_html_comment_parser_ignores_sidebars_and_scripts(self):
        parser = d.ThreadHTML("https://habr.com/ru/articles/123/")
        parser.feed('<aside>irrelevant</aside><div id="comment_9"><div class="tm-comment__body-content">needle <b>answer</b><script>bad</script></div></div>')
        self.assertEqual(parser.records, [{"url": "https://habr.com/ru/articles/123/#comment_9", "text": "needle answer"}])

    def test_forum_pagination_only_keeps_same_topic(self):
        parser = d.ThreadHTML("https://4pda.to/forum/index.php?showtopic=123")
        parser.feed('<a href="?showtopic=123&amp;st=200">last</a><a href="https://evil.test/?showtopic=123&amp;st=999">bad</a><a href="?showtopic=456&amp;st=900">other</a>')
        self.assertEqual(parser.pages, {200})

    def test_habr_loads_comments_page_and_preserves_article(self):
        session = self.session([{}, {}])
        # Real Habr separates the article and its comment bodies into two pages.
        article, comments = Mock(), Mock()
        article.text = '<div class="article-formatted-body">root article</div>'
        comments.text = '<div id="9"><a id="comment_9"></a><div class="tm-comment__body-content">answer</div></div>'
        session.get.side_effect = [article, comments]
        with patch.object(d, "GuestSession", return_value=session):
            result = d.read_discussion("https://habr.com/ru/articles/123/")
        self.assertIn("root article", result["text_content"])
        self.assertEqual(result["comment_evidence"][0]["url"], "https://habr.com/ru/articles/123/comments/#comment_9")
        self.assertEqual(session.get.call_count, 2)

    def test_format_keeps_latest_comment_as_evidence(self):
        comments = [{"url": f"https://github.com/org/repo/issues/1#issuecomment-{i}", "text": f"reply {i}"} for i in range(1,10)]
        block = search_tools.format_search_block([{"url": "https://github.com/org/repo/issues/1", "title": "needle", "body": "root", "content_kind": "discussion", "comment_evidence": comments, "thread_truncated": True}])
        self.assertIn("#issuecomment-9", block)
        self.assertIn("частично", block)

    def test_non_thread_url_is_not_rewritten_into_api_request(self):
        with patch.object(d, "GuestSession") as session:
            self.assertIsNone(d.read_discussion("https://github.com/org/repo"))
            self.assertIsNone(d.read_discussion("https://github.com.attacker.test/org/repo/issues/1"))
        session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
