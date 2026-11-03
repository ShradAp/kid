"""Settings integration with fake background workers and no actual network."""
import copy
import json
import sys
import types
import unittest
from unittest.mock import Mock, patch

from test_file_transfer import Request
from isolated_case import IsolatedStorageTestCase


class SettingsTests(IsolatedStorageTestCase):
    def setUp(self):
        from application import settings_profiles

        self.settings = settings_profiles

    def test_proxy_save_retains_password_and_reconnects_codex_separately_from_guest_agent(self):
        cfg = {"proxy_pass": "fixture-password"}
        load = lambda: copy.deepcopy(cfg)
        save = Mock()
        service = Mock()
        connect = Mock()
        assemble = Mock(return_value="http://fixture-user:fixture-password@127.0.0.1:9999")
        agent = Mock()
        agent.apply_proxy_settings.return_value = {"verified": True, "detail": "fixture"}
        modules = {
            "network_agent": types.ModuleType("network_agent"),
            "network_agent.proxy_env": types.SimpleNamespace(assemble_proxy=assemble),
            "network_agent.core": types.SimpleNamespace(agent_core=agent),
        }
        request = Request("/api/proxy-settings", json.dumps({
            "mode": "custom", "url": "http://127.0.0.1:9999",
            "username": "fixture-user", "password": "",
        }).encode())
        with patch.dict(sys.modules, modules), patch.object(self.settings.threading, "Thread") as thread:
            self.settings.save_proxy_settings(request, load, save, service, connect, Mock())
        self.assertEqual(request.status, 200)
        self.assertEqual(json.loads(request.wfile.getvalue())["verified"], True)
        self.assertEqual(save.call_args.args[0]["proxy_pass"], "fixture-password")
        service.close.assert_called_once_with()
        self.assertIs(thread.call_args.kwargs["target"], connect)
        thread.return_value.start.assert_called_once_with()
        agent.apply_proxy_settings.assert_called_once()
        self.assertEqual(agent.apply_proxy_settings.call_args.args[:4],
                         ("custom", "http://127.0.0.1:9999", "fixture-user", "fixture-password"))

    def test_invalid_proxy_mode_does_not_save_or_reconnect(self):
        save, service, connect = Mock(), Mock(), Mock()
        request = Request("/api/proxy-settings", b'{"mode":"invalid"}')
        self.settings.save_proxy_settings(request, lambda: {}, save, service, connect, Mock())
        self.assertEqual(request.status, 400)
        save.assert_not_called()
        service.close.assert_not_called()
        connect.assert_not_called()

    def test_prompt_generator_uses_injected_client_and_existing_payload(self):
        request = Request("/api/prompt-generator", json.dumps({
            "base_url": "https://provider.invalid/v1", "api_key": "fixture",
            "model": "model", "idea": "example", "system_prompt": "rules",
        }).encode())
        save = Mock()
        client = Mock(return_value={"content": "generated fixture"})
        self.settings.generate_prompt(request, lambda: {}, save, client)
        self.assertEqual(request.status, 200)
        self.assertEqual(json.loads(request.wfile.getvalue()), {"text": "generated fixture"})
        client.assert_called_once()
        self.assertEqual(client.call_args.kwargs["timeout"], 60)
        self.assertEqual(client.call_args.kwargs["messages"][0], {"role": "system", "content": "rules"})
        self.assertIn("example", client.call_args.kwargs["messages"][1]["content"])
        self.assertEqual(save.call_args.args[0]["prompt_generator"]["api_key"], "fixture")
