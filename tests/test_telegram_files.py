"""File transfer boundaries: real disk, offline Telegram and Codex stand-ins."""

import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import test_telegram as fixtures
from codex_engine import ProtocolError
from services.telegram_files import TelegramFiles, DOWNLOAD_LIMIT, UPLOAD_LIMIT
from services.telegram_transport import TelegramTransport, TelegramError


class FileTransport(fixtures.OfflineTelegram):
    def __init__(self):
        super().__init__()
        self.payload = b"Verification: FILE-ROUNDTRIP-42\n"
        self.documents = []
        self.downloads = []

    def call(self, method, **parameters):
        if method == "getFile":
            return {"file_path": "documents/file_1.txt", "file_size": len(self.payload)}
        if method == "sendDocument":
            self.documents.append({**parameters, "bytes": Path(parameters["document_path"]).read_bytes()})
            return {"document": {"file_id": "uploaded_fixture"}}
        return super().call(method, **parameters)

    def download(self, file_path, destination, cancel, max_bytes):
        self.downloads.append(file_path)
        if cancel.is_set():
            raise TelegramError("Загрузка остановлена")
        Path(destination).write_bytes(self.payload)


class FileTests(unittest.TestCase):
    wait = fixtures.TelegramTests.wait

    def setUp(self):
        fixtures.TelegramTests.setUp(self)
        self.transport = FileTransport()

    def document(self, identity=1, name="input.txt", size=50):
        update = fixtures.message(identity)
        update["message"].pop("text")
        update["message"]["caption"] = "Прочитай файл"
        update["message"]["document"] = {"file_id": "telegram_file", "file_name": name, "file_size": size}
        return update

    def test_incoming_document_is_downloaded_and_passed_as_actual_codex_input(self):
        self.bot.start()
        self.bot.accept_update(self.document(name="../../input.txt"))
        self.wait(lambda: bool(self.transport.sent))
        inputs = self.codex.get.return_value.run.call_args.kwargs["input_items"]
        self.assertIn("FILE-ROUNDTRIP-42", str(inputs))
        attachment = self.bot.store.state()["history"][0]["attachments"][0]
        self.assertTrue(Path(attachment["path"]).resolve().is_relative_to(self.bot.files.incoming.resolve()))
        self.assertEqual(Path(attachment["path"]).read_bytes(), self.transport.payload)
        self.assertEqual(self.codex.get.return_value.run.call_count, 1)

    def test_oversized_incoming_file_is_rejected_before_network_or_model(self):
        self.bot.accept_update(self.document(size=DOWNLOAD_LIMIT + 1))
        self.assertFalse(self.bot.store.state()["inbox"])
        self.assertIn("20 МБ", self.bot.store.state()["outbox"][0]["text"])
        self.assertFalse(self.transport.downloads)
        self.codex.get.assert_not_called()

    def test_photo_is_forwarded_as_local_image_not_a_claimed_text_description(self):
        import io
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (12, 12), "blue").save(output, format="PNG")
        self.transport.payload = output.getvalue()
        update = fixtures.message(1)
        update["message"].pop("text")
        update["message"]["photo"] = [{"file_id": "photo", "file_size": len(self.transport.payload)}]
        self.bot.start()
        self.bot.accept_update(update)
        self.wait(lambda: bool(self.transport.sent))
        inputs = self.codex.get.return_value.run.call_args.kwargs["input_items"]
        images = [item for item in inputs if item["type"] == "localImage"]
        self.assertEqual(len(images), 1)
        self.assertTrue(Path(images[0]["path"]).is_file())

    def test_unknown_file_type_is_passed_to_codex_for_tool_processing(self):
        self.bot.start()
        self.bot.accept_update(self.document(name="file.custom"))
        self.wait(lambda: bool(self.transport.sent))
        inputs = self.codex.get.return_value.run.call_args.kwargs["input_items"]
        attachment = self.bot.store.state()["history"][0]["attachments"][0]
        self.assertIn(attachment["path"], "\n".join(item.get("text", "") for item in inputs))
        self.assertIn("windows_command", str(inputs))
        self.assertNotIn("не поддерживается", str(inputs))
        self.assertNotIn("FILE-ROUNDTRIP-42", str(inputs))

    def test_outgoing_tool_sends_immutable_snapshot_and_receipt(self):
        source = self.root / "generated_files" / "reply.txt"
        source.parent.mkdir()
        source.write_bytes(b"original file")
        def run(session_id, text, operation, cancel, **kwargs):
            result = self.bot.queue_file("code", session_id, str(source), cancel)
            self.assertTrue(result["queued"])
            source.write_bytes(b"changed file")
            return {"reply": "Файл готов"}
        self.codex.get.return_value.run.side_effect = run
        self.bot.start()
        self.bot.accept_update(fixtures.message(1, "Пришли файл"))
        self.wait(lambda: bool(self.transport.documents) and bool(self.transport.sent))
        self.assertEqual(self.transport.documents[0]["bytes"], b"original file")
        receipt = self.bot.store.state()["last_sent_file"]
        self.assertEqual(receipt["file_id"], "uploaded_fixture")
        self.assertEqual(receipt["sha256"], hashlib.sha256(b"original file").hexdigest())

    def test_file_export_cannot_run_from_other_session_or_without_active_turn(self):
        with self.assertRaises(ProtocolError):
            self.bot.queue_file("code", "desktop", "C:/fake.txt", threading.Event())
        self.assertFalse(self.bot.store.state()["outbox"])

    def test_generated_codex_image_is_delivered_as_a_file(self):
        import io
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (8, 8), "green").save(output, format="PNG")
        identity = hashlib.sha256(output.getvalue()).hexdigest() + ".png"
        source = self.root / "generated_files" / "codex_images" / identity
        source.parent.mkdir(parents=True)
        source.write_bytes(output.getvalue())
        self.codex.get.return_value.run.return_value = {"reply": "Готово", "images": [{"url": "/api/codex-images/" + identity}]}
        self.bot.start()
        self.bot.accept_update(fixtures.message(1))
        self.wait(lambda: bool(self.transport.documents))
        self.assertEqual(self.transport.documents[0]["bytes"], output.getvalue())

    def test_private_configuration_is_never_exported_even_under_workspace(self):
        with self.assertRaisesRegex(ProtocolError, "Служебные"):
            self.bot.files.source(self.bot.store.settings_path)

    def test_file_errors_are_reported_to_model_without_false_proxy_failure(self):
        from application.codex_actions import execute_host_tool
        from tool_execution import ToolExecutionError
        cancel = threading.Event()
        session = Mock()
        session.lock = threading.RLock()
        session.active = {"thread": {"cancel": cancel, "session_id": "telegram_fixture"}}
        service = Mock()
        service.sessions = {"code": ("fingerprint", session)}
        service.telegram_export.side_effect = ProtocolError("Файл превышает 50 МБ")
        with self.assertRaisesRegex(ToolExecutionError, "50 МБ"):
            execute_host_tool("telegram_send_file", {"path": "C:/fixture.txt"}, "code", cancel,
                              service=service, workspaces=Mock(), load_config=lambda: {}, save_config=Mock(),
                              read_notes=Mock(), prepend_note=Mock())
        service.telegram_export.assert_called_once_with("code", "telegram_fixture", "C:/fixture.txt", cancel)

    def test_external_file_requires_approval_and_denial_never_queues_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "private.txt"
            source.write_text("fixture")
            session_id = self.bot.store.state()["session_id"]
            self.bot.current = (session_id, 1)
            self.bot.cancel = threading.Event()
            self.codex.approvals.wait.return_value = {"decision": "decline"}
            with self.assertRaisesRegex(ProtocolError, "не разрешена"):
                self.bot.queue_file("code", session_id, str(source), self.bot.cancel)
            self.codex.approvals.wait.assert_called_once()
            self.assertFalse(self.bot.store.state()["outbox"])

    def test_changed_snapshot_is_rejected_before_upload(self):
        source = self.root / "sample.txt"
        source.write_bytes(b"fixture")
        entry = self.bot.files.snapshot(source, threading.Event())
        Path(entry["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ProtocolError, "изменён"):
            self.bot.files.validate_snapshot(entry)

    def test_symlink_to_private_configuration_is_not_exported(self):
        link = self.root / "generated_files" / "alias.txt"
        link.parent.mkdir()
        try:
            link.symlink_to(self.bot.store.settings_path)
        except OSError:
            self.skipTest("Symlink creation not available")
        with self.assertRaises(ProtocolError):
            self.bot.files.source(link)


class DownloadTests(unittest.TestCase):
    def test_download_size_is_bounded_even_without_content_length(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "file.txt"
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.status_code = 200
            response.headers = {}
            response.iter_content.return_value = [b"abc", b"def"]
            client = Mock()
            client.get.return_value = response
            transport = TelegramTransport({"token": fixtures.TOKEN}, directory)
            with patch.object(transport, "session", return_value=client):
                with self.assertRaisesRegex(TelegramError, "20 МБ"):
                    transport.download("documents/file.txt", target, threading.Event(), max_bytes=4)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".txt.part").exists())
            self.assertFalse(client.get.call_args.kwargs["allow_redirects"])

    def test_download_does_not_accept_external_urls_or_parent_paths(self):
        transport = TelegramTransport({"token": fixtures.TOKEN}, ".")
        for path in ("https://example.com/file", "../private", "/absolute", "documents/%2e%2e/file", "file?token=bad"):
            with self.subTest(path=path), self.assertRaises(TelegramError), patch.object(transport, "session") as session:
                transport.download(path, "unused.txt", threading.Event())
            session.assert_not_called()

    def test_multipart_upload_keeps_filename_and_closes_handle(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixture.txt"
            source.write_bytes(b"roundtrip")
            client = Mock()
            client.post.return_value.status_code = 200
            client.post.return_value.json.return_value = {"ok": True, "result": {"document": {"file_id": "receipt"}}}
            captured = []
            def post(*args, **kwargs):
                name, handle, mime = kwargs["files"]["document"]
                captured.append((name, handle.read(), handle))
                self.assertEqual(kwargs["data"]["chat_id"], fixtures.OWNER)
                return client.post.return_value
            client.post.side_effect = post
            transport = TelegramTransport({"token": fixtures.TOKEN}, directory)
            with patch.object(transport, "session", return_value=client):
                result = transport.call("sendDocument", chat_id=fixtures.OWNER, document_path=str(source), filename="Тест.txt")
            self.assertEqual(captured[0][:2], ("Тест.txt", b"roundtrip"))
            self.assertTrue(captured[0][2].closed)
            self.assertEqual(result["document"]["file_id"], "receipt")


if __name__ == "__main__":
    unittest.main()
