"""File-transfer payload and header regressions without native dialogs."""
from email.message import Message
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from isolated_case import IsolatedStorageTestCase


class Request:
    def __init__(self, path, body=b"", content_type="application/json"):
        self.path = path
        self.headers = Message()
        self.headers["Content-Length"] = str(len(body))
        self.headers["Content-Type"] = content_type
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.outgoing = {}
        self.status = None

    def _set_cors(self, status=200):
        self.status = status

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.outgoing[name] = value

    def end_headers(self):
        pass


class FileTransferTests(IsolatedStorageTestCase):
    def setUp(self):
        from application import file_transfer

        self.transfer = file_transfer

    def test_first_text_multipart_part_roundtrips_as_attachment(self):
        body = (b"--fixture\r\nContent-Disposition: form-data; name=\"file\"; filename=\"example.txt\"\r\n"
                b"Content-Type: text/plain\r\n\r\nfirst text part\r\n--fixture--\r\n")
        with tempfile.TemporaryDirectory(prefix="kid-file-transfer-") as directory, \
             patch.object(self.transfer.tempfile, "gettempdir", return_value=directory):
            upload = Request("/api/upload", body, "multipart/form-data; boundary=fixture")
            self.transfer.upload(upload)
            self.assertEqual(upload.status, 200)
            payload = json.loads(upload.wfile.getvalue())
            self.assertEqual(payload["name"], "example.txt")
            self.assertEqual(payload["mime"], "text/plain")
            download = Request("/api/download/" + payload["file_id"])
            self.transfer.download_upload(download)
            self.assertEqual(download.status, 200)
            self.assertEqual(download.wfile.getvalue(), b"first text part")
            self.assertEqual(download.outgoing["Content-Length"], "15")
            self.assertIn('filename="example.txt"', download.outgoing["Content-Disposition"])

    def test_local_download_denies_unselected_path(self):
        from bridge_security import FileAccess

        request = Request("/api/download-local?path=private.txt")
        self.transfer.download_local(request, FileAccess())
        self.assertEqual(request.status, 404)
        self.assertIn("error", json.loads(request.wfile.getvalue()))

    def test_unicode_download_name_has_ascii_header_and_utf8_parameter(self):
        header = self.transfer._content_disposition('Отчёт "2026".docx')
        header.encode("ascii")
        self.assertIn("filename*=UTF-8''", header)
        self.assertIn("%D0", header)

    def test_reveal_local_selects_file_without_running_it(self):
        with tempfile.TemporaryDirectory(prefix="kid-reveal-") as directory:
            path = Path(directory, "Отчёт (1), готово.pyw")
            path.touch()
            request = Request("/api/reveal-local", json.dumps({"path": path.as_uri()}).encode())
            with patch.object(self.transfer.subprocess, "Popen") as launch:
                self.transfer.reveal_local(request)
            self.assertEqual(request.status, 200)
            self.assertEqual(json.loads(request.wfile.getvalue()), {"ok": True})
            if os.name == "nt":
                self.assertIn(f'/select,"{path}"', launch.call_args.args[0])
                self.assertFalse(launch.call_args.kwargs["shell"])
            launch.assert_called_once()

    def test_reveal_local_opens_directory_itself(self):
        with tempfile.TemporaryDirectory(prefix="kid-reveal-") as directory:
            request = Request("/api/reveal-local", json.dumps({"path": directory}).encode())
            with patch.object(self.transfer.subprocess, "Popen") as launch:
                self.transfer.reveal_local(request)
            self.assertEqual(request.status, 200)
            launch.assert_called_once()
            if os.name == "nt":
                self.assertNotIn("/select,", launch.call_args.args[0])
                self.assertTrue(launch.call_args.args[0].endswith(f'"{directory}"'))

    @unittest.skipUnless(os.name == "nt", "Windows Codex paths")
    def test_codex_link_paths_with_source_locations(self):
        path = r"C:\Users\Ivan\Рабочий стол\Отчёт (1).txt"
        for reference in ("</" + path.replace("\\", "/") + ">",
                          "/" + path.replace("\\", "/") + ":12:3",
                          Path(path).as_uri() + "#L12C3"):
            with self.subTest(reference=reference):
                self.assertEqual(self.transfer._local_link_path(reference), path)

    def test_reveal_local_rejects_invalid_or_missing_paths(self):
        for payload in ({"path": "relative.txt"}, {"path": "https://example.com/file"},
                        {"path": "file://remote-host/share/file"}, {"path": "bad\x00path"},
                        {"path": 42}, [], {}):
            with self.subTest(payload=payload), patch.object(self.transfer.subprocess, "Popen") as launch:
                request = Request("/api/reveal-local", json.dumps(payload).encode())
                self.transfer.reveal_local(request)
                self.assertEqual(request.status, 400)
                launch.assert_not_called()
        with tempfile.TemporaryDirectory(prefix="kid-reveal-") as directory:
            request = Request("/api/reveal-local", json.dumps({"path": str(Path(directory, "missing.txt"))}).encode())
            with patch.object(self.transfer.subprocess, "Popen") as launch:
                self.transfer.reveal_local(request)
                self.assertEqual(request.status, 404)
                launch.assert_not_called()
