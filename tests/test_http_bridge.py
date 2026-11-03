"""HTTP boundary regressions independent of provider/network execution."""
import json
import threading
from unittest.mock import Mock, patch

from isolated_case import IsolatedStorageTestCase
from test_file_transfer import Request


class HttpBridgeTests(IsolatedStorageTestCase):
    def setUp(self):
        import desktop_app

        self.app = desktop_app

    def test_authorization_uses_current_bound_port_and_rejects_origin(self):
        request = Request("/api/init")
        request.headers["Host"] = "127.0.0.1:51837"
        request.headers["X-Kid-Token"] = self.app.BRIDGE_TOKEN
        with patch.object(self.app, "BRIDGE_PORT", 51837):
            self.assertTrue(self.app.BridgeHandler._authorized(request))
            request.headers["Origin"] = "http://external.invalid"
            self.assertFalse(self.app.BridgeHandler._authorized(request))
        self.assertEqual(request.status, 403)
        self.assertTrue(request.close_connection)

    def post_request(self, error):
        request = Request("/api/chat")
        request._authorized = lambda: True
        request._chat_session_id = "fixture"
        request._do_POST = Mock(side_effect=error)
        self.app.LIVE_THINKING["fixture"] = ["working"]
        self.app.LIVE_REASONING["fixture"] = "reasoning"
        cancel = threading.Event()
        self.app.CANCEL_EVENTS["fixture"] = cancel
        self.app.BridgeHandler.do_POST(request)
        self.assertNotIn("fixture", self.app.LIVE_THINKING)
        self.assertNotIn("fixture", self.app.LIVE_REASONING)
        self.assertNotIn("fixture", self.app.CANCEL_EVENTS)
        return request, cancel

    def test_disconnected_client_sets_cancel_and_clears_progress(self):
        request, cancel = self.post_request(BrokenPipeError("fixture closed"))
        self.assertTrue(cancel.is_set())
        self.assertEqual(request.wfile.getvalue(), b"")

    def test_unexpected_failure_delivers_json_and_keeps_original_cancel_semantics(self):
        request, cancel = self.post_request(RuntimeError("fixture failure"))
        self.assertEqual(request.status, 500)
        self.assertEqual(json.loads(request.wfile.getvalue()), {"error": "fixture failure"})
        self.assertFalse(cancel.is_set())

    def test_oversized_request_is_rejected_before_dispatch(self):
        request = Request("/api/chat")
        request.headers.replace_header("Content-Length", str(9 * 1024 * 1024))
        request._authorized = lambda: True
        request._do_POST = Mock()
        self.app.BridgeHandler.do_POST(request)
        request._do_POST.assert_not_called()
        self.assertEqual(request.status, 413)
        self.assertTrue(request.close_connection)

    def test_reveal_local_route_dispatches_to_file_transfer(self):
        from application import file_transfer

        request = Request("/api/reveal-local", b'{"path":"C:/work/result.txt"}')
        request._authorized = lambda: True
        request._do_POST = lambda: self.app.BridgeHandler._do_POST(request)
        with patch.object(file_transfer, "reveal_local") as reveal:
            self.app.BridgeHandler.do_POST(request)
        reveal.assert_called_once_with(request)

    def test_telegram_routes_use_authenticated_bridge_and_mask_saved_token(self):
        from application import telegram_actions

        request = Request("/api/telegram/settings")
        request._authorized = Mock(return_value=False)
        with patch.object(telegram_actions, "handle") as handle:
            self.app.BridgeHandler.do_GET(request)
        handle.assert_not_called()
        request._authorized = Mock(return_value=True)
        with patch.object(self.app.TELEGRAM, "public_settings", return_value={"has_token": True}):
            self.app.BridgeHandler.do_GET(request)
        self.assertEqual(json.loads(request.wfile.getvalue()), {"has_token": True})

    def test_telegram_invalid_json_does_not_echo_secret(self):
        from application import telegram_actions

        request = Request("/api/telegram/settings", b'{"token":"fixture-secret", broken}')
        telegram_actions.handle(request, Mock(), "POST")
        self.assertEqual(request.status, 400)
        self.assertNotIn(b"fixture-secret", request.wfile.getvalue())
