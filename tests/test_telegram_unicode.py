"""Catch the Windows terminal's irreversible Cyrillic-to-question-mark conversion."""

import unittest
from unittest.mock import Mock, patch

from scripts.check_telegram_format import SAMPLE, assert_cyrillic
from services.telegram_format import format_reply
from services.telegram_transport import TelegramTransport


class TelegramUnicodeTests(unittest.TestCase):
    def test_utf8_source_formatting_and_http_json_preserve_cyrillic(self):
        chunks = list(format_reply(SAMPLE))
        assert_cyrillic(chunks)
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {"ok": True, "result": True}
        with patch("services.telegram_transport.requests.Session", return_value=session):
            transport = TelegramTransport({"token": "fixture-token"}, ".")
            try:
                transport.call("sendRichMessage", chat_id=1, rich_message={"html": chunks[0]["rich_html"]})
                assert_cyrillic(session.post.call_args.kwargs["json"])
            finally:
                transport.close_thread()

    def test_diagnostic_rejects_damaged_source_or_server_response(self):
        damaged = SAMPLE.encode("ascii", errors="replace").decode("ascii")
        with self.assertRaisesRegex(AssertionError, "Cyrillic text was lost"):
            assert_cyrillic(list(format_reply(damaged)))
        with self.assertRaises(AssertionError):
            assert_cyrillic({"rich_message": {"blocks": [{"text": "KID: ???????"}]}})
