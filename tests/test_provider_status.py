"""Provider status uses mocked transports; no OpenAI or other remote calls."""
import io
import time
import unittest
from unittest.mock import Mock, patch

from application.provider_status import ProviderStatus


class ProviderStatusTests(unittest.TestCase):
    def setUp(self):
        self.opener = Mock()
        self.log = Mock()
        self.status = ProviderStatus(self.opener, self.log)

    def test_first_fetch_populates_cache_and_fresh_cache_avoids_network(self):
        response = Mock(status_code=200)
        response.json.return_value = {"data": {"balance": "12.5"}}
        with patch("application.provider_status.requests.get", return_value=response) as get:
            self.assertEqual(self.status.balance("fixture-key"), 12.5)
            self.assertEqual(self.status.balance("fixture-key"), 12.5)
            self.assertEqual(self.status.balance(""), 0.0)
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], "https://sprutdock.ru/v1/users/balance")

    def test_stale_value_is_returned_before_background_refresh_and_failure_keeps_it(self):
        self.status.last_known["value"] = 15.0
        self.status.cache.update(value=15.0, ts=time.time() - 60, key="fixture-key")
        with patch.object(self.status, "start_refresh") as start, \
             patch.object(self.status, "fetch") as fetch:
            self.assertEqual(self.status.balance("fixture-key"), 15.0)
            start.assert_called_once_with("fixture-key")
            fetch.assert_not_called()
        with patch("application.provider_status.requests.get", side_effect=OSError("fixture failure")) as get:
            self.assertEqual(self.status.balance("fixture-key", force=True), 15.0)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs["proxies"], {"http": None, "https": None})

    def test_background_worker_releases_running_flag_on_error_and_throttles_retries(self):
        with patch("application.provider_status.threading.Thread") as thread, \
             patch.object(self.status, "fetch", side_effect=RuntimeError("fixture")):
            self.assertTrue(self.status.start_refresh("key"))
            self.assertTrue(self.status.start_refresh("key"))
            thread.assert_called_once()
            with self.assertRaises(RuntimeError):
                thread.call_args.kwargs["target"]()
            self.assertFalse(self.status.refresh_state["running"])
            self.assertFalse(self.status.start_refresh("key"))

    def test_models_keep_profile_url_and_payload_and_return_empty_on_error(self):
        response = io.BytesIO(b'{"data":[{"id":"one","owned_by":"fixture"},{"id":"two"},{}]}')
        self.opener.open.return_value = response
        result = self.status.models({"base_url": "https://provider.invalid/v1/", "api_key": "fixture-key"})
        self.assertEqual(result, {"models": [
            {"id": "one", "name": "one", "owned_by": "fixture"},
            {"id": "two", "name": "two", "owned_by": "SprutDock"},
        ]})
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://provider.invalid/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-key")
        self.opener.open.side_effect = OSError("fixture failure")
        self.assertEqual(self.status.models({}), {"models": []})
