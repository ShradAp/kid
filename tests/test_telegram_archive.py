"""Archive persistence, provenance, exact lines, owner boundaries and reply selection."""

from datetime import datetime, timedelta
from pathlib import Path
import threading
import unittest
import uuid

import test_telegram as fixture
import test_telegram_files as file_fixture
from codex_engine import ProtocolError


class ArchiveTests(unittest.TestCase):
    setUp = fixture.TelegramTests.setUp
    wait = fixture.TelegramTests.wait

    def save_file(self, name="test.txt", content="first\nsecond\nthird\nfourth\n", **metadata):
        root = self.bot.files.incoming
        root.mkdir(parents=True, exist_ok=True)
        path = root / (uuid.uuid4().hex + "_" + name)
        path.write_text(content, encoding="utf-8", newline="")
        entry = self.bot.files.archive.register(self.settings, path, name, {"file_id":"fixture-id"}, metadata)
        return path, entry

    def test_archive_survives_clear_and_reads_exact_third_line(self):
        path, entry = self.save_file(content="первая\r\n\r\nтретья\r\n")
        old = self.bot.store.state()["session_id"]
        self.bot.clear()
        self.assertNotEqual(old, self.bot.store.state()["session_id"])
        found = self.bot.files.archive.search(self.settings, name="TEST.txt")
        self.assertEqual(found["total"], 1)
        result = self.bot.files.archive.read(self.settings, entry["archive_id"], 3, 1)
        self.assertEqual(result["lines"], [{"number":3, "text":"третья"}])
        self.assertEqual(result["total_lines"], 3)
        self.assertTrue(path.is_file())

    def test_versions_are_separate_and_date_filter_selects_original_sent_day(self):
        today = datetime.now().astimezone()
        yesterday = today - timedelta(days=1)
        self.save_file(sent_at=yesterday.timestamp(), message_id=10, caption="Old version")
        _, latest = self.save_file(content="new", sent_at=today.timestamp(), message_id=11)
        result = self.bot.files.archive.search(self.settings, name="test.txt")
        self.assertEqual(result["total"], 2)
        self.assertTrue(result["selection_required"])
        day = yesterday.date().isoformat()
        filtered = self.bot.files.archive.search(self.settings, name="test.txt", date_from=day, date_to=day)
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["matches"][0]["caption"], "Old version")
        path, selected = self.bot.files.archive.resolve(self.settings, message_id=11)
        self.assertEqual(selected["archive_id"], latest["archive_id"])
        self.assertEqual(path.read_text(), "new")

    def test_other_owner_or_bot_cannot_search_read_or_delete_files(self):
        path, entry = self.save_file()
        for changed in ({**self.settings, "owner_id":"999"}, {**self.settings, "token":"999999:fixture"}):
            self.assertEqual(self.bot.files.archive.search(changed)["total"], 0)
            with self.assertRaises(ProtocolError):
                self.bot.files.archive.read(changed, entry["archive_id"])
            self.assertEqual(self.bot.files.archive.delete(changed), 0)
            self.assertTrue(path.is_file())

    def test_catalogue_does_not_allow_path_traversal_and_detects_changed_bytes(self):
        path, entry = self.save_file()
        path.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ProtocolError, "изменён"):
            self.bot.files.archive.read(self.settings, entry["archive_id"])
        archive = self.bot.files.archive
        index = archive._load()
        index["entries"][0]["stored_name"] = "../../../../telegram.json"
        from services.telegram_storage import TelegramStorage
        TelegramStorage.write(archive.index_path, index)
        with self.assertRaises(ProtocolError):
            archive.read(self.settings, entry["archive_id"])
        with self.assertRaises(ProtocolError):
            archive.delete(self.settings)
        self.assertTrue(self.bot.store.settings_path.is_file())

    def test_legacy_import_is_one_time_and_dates_are_marked_approximate(self):
        path, _ = self.save_file()
        self.bot.files.archive.index_path.unlink()
        archive = self.bot.files.archive
        archive.migrate(self.settings)
        result = archive.search(self.settings)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["matches"][0]["date_source"], "file_mtime_approximate")
        other = {**self.settings, "owner_id":"999"}
        archive.migrate(other)
        self.assertEqual(archive.search(other)["total"], 0)

    def test_reading_is_bounded_and_rejects_invalid_ranges_and_dates(self):
        _, entry = self.save_file(content="x" * 40000)
        result = self.bot.files.archive.read(self.settings, entry["archive_id"])
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["lines"][0]["text"]), 24000)
        for start, count in [(0, 1), (1, 201), (True, 1), (1, False)]:
            with self.assertRaises(ProtocolError):
                self.bot.files.archive.read(self.settings, entry["archive_id"], start, count)
        with self.assertRaises(ProtocolError):
            self.bot.files.archive.search(self.settings, date_from="yesterday")

    def test_host_tools_require_current_session_and_cancel_event(self):
        _, entry = self.save_file()
        cancel = threading.Event()
        sid = self.bot.store.state()["session_id"]
        with self.assertRaises(ProtocolError):
            self.bot.archive_tool("code", sid, cancel, "telegram_find_files", {})
        self.bot.current, self.bot.cancel = (sid, 1), cancel
        result = self.bot.archive_tool("code", sid, cancel, "telegram_read_file", {"archive_id":entry["archive_id"], "start_line":3, "line_count":1})
        self.assertEqual(result["lines"][0]["text"], "third")
        self.bot.clear()
        with self.assertRaises(ProtocolError):
            self.bot.archive_tool("code", sid, cancel, "telegram_find_files", {})

    def test_explicit_archive_delete_removes_files_and_resets_context(self):
        path, entry = self.save_file()
        before = self.bot.store.state()["session_id"]
        self.assertEqual(self.bot.clear_archive(), 1)
        self.assertFalse(path.exists())
        self.assertEqual(self.bot.files.archive.search(self.settings)["total"], 0)
        self.assertNotEqual(self.bot.store.state()["session_id"], before)

    def test_reply_to_old_file_preserves_question_and_reuses_verified_original(self):
        self.transport = file_fixture.FileTransport()
        self.transport.payload = b"first\nsecond\nEXACT-THIRD-LINE\n"
        update = file_fixture.FileTests.document(self, name="test.txt")
        update["message"]["message_id"] = 111
        self.bot.start()
        self.bot.accept_update(update)
        self.wait(lambda: bool(self.transport.sent))
        self.wait(lambda: not self.bot.store.state()["outbox"])
        self.bot.clear()
        question = fixture.message(2, "Read the third line of the file I replied to")
        question["message"]["reply_to_message"] = update["message"]
        self.bot.accept_update(question)
        self.wait(lambda: self.codex.get.return_value.run.call_count == 2 and not self.bot.current)
        call = self.codex.get.return_value.run.call_args
        self.assertEqual(call.args[1], "Read the third line of the file I replied to")
        self.assertIn("EXACT-THIRD-LINE", str(call.kwargs["input_items"]))
        self.assertEqual(len(self.transport.downloads), 1)
        self.assertEqual(self.bot.public_settings()["archive_count"], 1)

    def test_files_command_does_not_run_model(self):
        self.save_file()
        self.bot.start()
        self.bot.accept_update(fixture.message(1, "/files"))
        self.wait(lambda: bool(self.transport.sent))
        self.assertIn("test.txt", self.transport.sent[0]["text"])
        self.codex.get.assert_not_called()
