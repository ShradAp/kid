import json
import threading
import unittest
from unittest.mock import Mock

from application.chat_progress import ChatProgress
from test_file_transfer import Request


class ProgressTests(unittest.TestCase):
    def test_snapshots_and_stop_are_scoped_to_one_session(self):
        progress = ChatProgress()
        first, second = threading.Event(), threading.Event()
        progress.cancel_events.update(first=first, second=second)
        progress.thinking["first"] = ["step"]
        progress.reasoning["first"] = "reasoning fixture"
        progress.reply["first"] = "partial"
        request = Request("/api/progress?session_id=first")
        progress.write_snapshot(request)
        self.assertEqual(json.loads(request.wfile.getvalue()), {
            "active": True, "steps": ["step"], "reasoning": "reasoning fixture", "reply": "partial",
        })
        stop = Request("/api/stop", b'{"session_id":"first"}')
        progress.stop(stop, Mock())
        self.assertTrue(first.is_set())
        self.assertFalse(second.is_set())
        self.assertEqual(json.loads(stop.wfile.getvalue()), {"ok": True, "stopped": True})

    def test_missing_progress_and_malformed_stop_keep_existing_contract(self):
        progress = ChatProgress()
        request = Request("/api/progress?session_id=missing")
        progress.write_snapshot(request)
        self.assertEqual(json.loads(request.wfile.getvalue()), {
            "active": False, "steps": [], "reasoning": "", "reply": "",
        })
        stop = Request("/api/stop", b"not json")
        progress.stop(stop, Mock())
        self.assertEqual(json.loads(stop.wfile.getvalue()), {"ok": True, "stopped": False})
