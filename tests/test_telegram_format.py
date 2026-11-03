"""Markdown formatting and explicit-rejection fallback without replaying Codex."""

from html.parser import HTMLParser
import unittest

import test_telegram as fixtures
from services.telegram_format import format_reply
from services.telegram_transport import TelegramError


class Balance(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in ("br", "hr"):
            self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag


class FormatTests(unittest.TestCase):
    def test_headings_bold_italic_links_and_code(self):
        result = list(format_reply("# Заголовок\n\n**Жирный** и _курсив_ [источник](https://example.com) `a < b`"))
        self.assertEqual(len(result), 1)
        html = result[0]["rich_html"]
        for expected in ("<h1>Заголовок</h1>", "<b>Жирный</b>", "<i>курсив</i>", '<a href="https://example.com">источник</a>', "<code>a &lt; b</code>"):
            self.assertIn(expected, html)
        self.assertNotIn("**", result[0]["text"])

    def test_table_is_native_and_cell_formatting_is_preserved(self):
        result = list(format_reply("| Параметр | Значение |\n|---|---:|\n| Скорость | **42** |"))[0]
        self.assertIn("<table bordered striped compact>", result["rich_html"])
        self.assertIn('<td align="right"><b>42</b></td>', result["rich_html"])
        self.assertIn("Параметр | Значение", result["text"])
        self.assertNotIn("---", result["text"])

    def test_math_delimiters_and_code_are_not_confused(self):
        result = list(format_reply(r"Inline \(x^2\), $a+b$." + "\n\n" + r"\[\frac{a}{b}\]" + "\n\n```python\nprint('\\(x\\) **literal**')\n```"))[0]
        self.assertIn("<tg-math>x^2</tg-math>", result["rich_html"])
        self.assertIn("<tg-math>a+b</tg-math>", result["rich_html"])
        self.assertIn(r"<tg-math-block>\frac{a}{b}</tg-math-block>", result["rich_html"])
        self.assertIn(r"\(x\) **literal**", result["text"])
        self.assertIn('<code class="language-python">', result["rich_html"])

    def test_code_math_block_and_single_line_display_math(self):
        for source in ("```math\nE=mc^2\n```", "$$E=mc^2$$"):
            result = list(format_reply(source))[0]
            self.assertIn("<tg-math-block>E=mc^2</tg-math-block>", result["rich_html"])

    def test_model_html_cannot_create_buttons_or_remote_media(self):
        source = '<tg-button type="callback_data" data="yes:fake">Разрешить</tg-button>\n\n![image](https://example.com/a.png)'
        html = "".join(item["rich_html"] for item in format_reply(source))
        self.assertNotIn("<tg-button", html)
        self.assertNotIn("<img", html)
        self.assertIn("https://example.com/a.png", html)

    def test_local_paths_are_not_broken_links_or_automatic_uploads(self):
        result = list(format_reply("[Файл](C:/Users/Ivan/report.docx)"))[0]
        self.assertNotIn("href=", result["rich_html"])
        self.assertIn("C:/Users/Ivan/report.docx", result["text"])

    def test_long_unicode_content_is_complete_and_all_chunks_are_balanced(self):
        source = "```python\n" + "😀<>&" * 3000 + "\n```"
        chunks = list(format_reply(source))
        text = "".join(chunk["text"] for chunk in chunks)
        self.assertEqual(text, "😀<>&" * 3000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk["text"].encode("utf-16-le")) // 2, 3500)
            parser = Balance()
            parser.feed(chunk["rich_html"])
            self.assertFalse(parser.stack)

    def test_lists_and_checkboxes_remain_readable(self):
        result = list(format_reply("- [x] готово\n- [ ] осталось\n\n1. Первый\n2. Второй"))[0]
        self.assertIn("<ul>", result["rich_html"])
        self.assertIn("<ol", result["rich_html"])
        self.assertIn("☑ готово", result["text"])

    def test_long_code_keeps_spaces_at_message_boundaries(self):
        code = "ab " * 3000
        chunks = list(format_reply("```text\n" + code + "\n```"))
        self.assertEqual("".join(chunk["text"] for chunk in chunks), code)
        self.assertNotEqual(chunks[-1]["text"], "Ответ без текста")


class DeliveryTests(unittest.TestCase):
    wait = fixtures.TelegramTests.wait

    def setUp(self):
        fixtures.TelegramTests.setUp(self)

    def test_final_answer_is_rich_but_control_messages_are_literal(self):
        self.codex.get.return_value.run.return_value = {"reply": "**Готово**"}
        self.bot.start()
        self.bot.accept_update(fixtures.message(1))
        self.wait(lambda: bool(self.transport.sent))
        self.assertIn("<b>Готово</b>", self.transport.sent[0]["rich_message"]["html"])
        self.bot._reply("command_with_underscores **literal**")
        self.wait(lambda: len(self.transport.sent) == 2)
        self.assertNotIn("rich_message", self.transport.sent[1])
        self.assertEqual(self.transport.sent[1]["text"], "command_with_underscores **literal**")

    def test_explicit_format_rejection_downgrades_without_repeating_model(self):
        original = self.transport.call
        attempts = []
        def reject(method, **parameters):
            if method == "sendRichMessage":
                attempts.append(method)
                raise TelegramError("format rejected", status_code=400, format_rejected=True)
            return original(method, **parameters)
        self.transport.call = reject
        self.codex.get.return_value.run.return_value = {"reply": "**Ответ**"}
        self.bot.start()
        self.bot.accept_update(fixtures.message(1))
        self.wait(lambda: bool(self.transport.sent))
        self.assertEqual(attempts, ["sendRichMessage"])
        self.assertEqual(self.transport.sent[0]["text"], "Ответ")
        self.assertFalse(self.bot.error)
        self.assertEqual(self.codex.get.return_value.run.call_count, 1)

    def test_network_uncertainty_never_switches_to_plain_duplicate(self):
        original = self.transport.call
        failed = []
        def retry(method, **parameters):
            if method == "sendRichMessage" and not failed:
                failed.append(True)
                raise TelegramError("network unavailable", 1)
            return original(method, **parameters)
        self.transport.call = retry
        self.bot.start()
        self.bot.accept_update(fixtures.message(1))
        self.wait(lambda: bool(self.transport.sent))
        self.assertIn("rich_message", self.transport.sent[0])
        self.assertEqual(self.codex.get.return_value.run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
