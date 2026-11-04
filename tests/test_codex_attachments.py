import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from bridge_security import FileAccess
from codex_attachments import prepare_attachments
from codex_engine import ProtocolError
from codex_proxy import effective_profile, global_endpoint, ProxyUnavailable


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.access = FileAccess((self.root,))

    def test_uploaded_image_uses_local_input_and_persistable_preview(self):
        from PIL import Image
        identifier = uuid.uuid4().hex
        path = self.root / (identifier + "_image.png")
        Image.new("RGB", (30, 20), "blue").save(path)
        items, history = prepare_attachments([{"file_id": identifier, "name": "image.png"}], self.access, {}, self.root)
        self.assertEqual(items, [{"type": "localImage", "path": str(path)}])
        self.assertTrue(history[0]["isImage"])
        self.assertIn("download-local?path=", history[0]["url"])

    def test_documents_are_read_and_ungranted_paths_are_blocked(self):
        path = self.root / "example.md"
        path.write_text("hello attachment", encoding="utf-8")
        items, _ = prepare_attachments([{"path": str(path)}], self.access, {})
        self.assertIn("hello attachment", items[0]["text"])
        self.assertIn(str(path), items[0]["text"])
        with self.assertRaises(ProtocolError):
            prepare_attachments([{"path": str(path)}], FileAccess(), {})

    def test_arbitrary_formats_preserve_original_bytes_and_pass_readable_paths(self):
        for name, content in (("схема.drawio", b'<mxfile><diagram>compressed-data</diagram></mxfile>'),
                              ("drawing.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>'),
                              ("source.tsx", b"export const App = () => null;"),
                              ("archive.zip", b"PK\x03\x04\x00\xff"), ("LICENSE", b"license")):
            for uploaded in (False, True):
                with self.subTest(name=name, uploaded=uploaded):
                    identifier = uuid.uuid4().hex
                    path = self.root / ((identifier + "_" if uploaded else "") + name)
                    path.write_bytes(content)
                    attachment = {"name": name, **({"file_id": identifier} if uploaded else {"path": str(path)})}
                    items, history = prepare_attachments([attachment], self.access, {}, self.root)
                    self.assertEqual(items[0]["type"], "text")
                    self.assertIn(str(path), items[0]["text"])
                    self.assertIn("windows_command", items[0]["text"])
                    self.assertEqual(path.read_bytes(), content)
                    self.assertEqual(history[0]["name"], name)
                    self.assertFalse(history[0]["isImage"])
                    with self.assertRaises(ProtocolError):
                        prepare_attachments([attachment], FileAccess(), {}, self.root)

    def test_failed_or_empty_preview_keeps_original_available(self):
        for content, error in (("", "parser unavailable"), ("", None)):
            with self.subTest(error=error):
                path = self.root / "document.pdf"
                path.write_bytes(b"original")
                with patch("attach_tools.read_document", return_value=(content, 0, error)):
                    items, _ = prepare_attachments([{"path": str(path)}], self.access, {})
                self.assertIn(str(path), items[0]["text"])
                self.assertIn("Предпросмотр недоступен", items[0]["text"])

    def test_preview_budget_does_not_block_later_attachments(self):
        paths = [self.root / f"document{index}.txt" for index in range(4)]
        for path in paths:
            path.write_text("x" * 70000, encoding="utf-8")
        items, history = prepare_attachments([{"path": str(path)} for path in paths], self.access, {})
        self.assertEqual(len(history), 4)
        for path, item in zip(paths, items):
            self.assertIn(str(path), item["text"])
        self.assertNotIn("x" * 100, items[-1]["text"])

    def test_invalid_missing_and_corrupt_files_fail_before_model(self):
        (self.root / "corrupt.png").write_text("not an image")
        for attachment in ({"file_id": "../example"}, {"file_id": uuid.uuid4().hex},
                           {"path": str(self.root / "corrupt.png")}, {"url": "https://example.com/image.png"}):
            with self.subTest(attachment=attachment), self.assertRaises(ProtocolError):
                prepare_attachments([attachment], self.access, {}, self.root)

    def test_image_limit_is_enforced_for_native_picker_too(self):
        from PIL import Image
        path = self.root / "image.png"
        Image.new("RGB", (3, 3)).save(path)
        with self.assertRaises(ProtocolError):
            prepare_attachments([{"path": str(path)}] * 2, self.access, {"attach_max_images": 1})


class GlobalProxyTests(unittest.TestCase):
    def test_global_credentials_override_every_profile_without_mutation(self):
        cfg = {"proxy_mode": "custom", "proxy_url": "socks5://127.0.0.1:8000", "proxy_user": "global-user", "proxy_pass": "private"}
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "config.json").write_text(json.dumps(cfg), encoding="utf-8")
            profile = {"codex": {"model": "chosen", "proxy": "http://127.0.0.1:9999", "proxy_password": "old"}}
            effective = effective_profile(profile, directory)
            self.assertEqual(effective["codex"]["proxy"], "socks5h://global-user@127.0.0.1:8000")
            self.assertEqual(effective["codex"]["proxy_password"], "private")
            self.assertEqual(profile["codex"]["proxy_password"], "old")
            self.assertEqual(effective["codex"]["model"], "chosen")

    def test_direct_missing_and_invalid_never_dial_or_fall_back(self):
        with patch("socket.socket") as dial:
            for cfg in ({"proxy_mode": "direct", "proxy_url": "http://127.0.0.1:8000"},
                        {"proxy_mode": "custom"}, {"proxy_mode": "custom", "proxy_url": "http://example.com:8000"}):
                with self.assertRaises(ProxyUnavailable):
                    global_endpoint("unused", cfg)
            with patch("urllib.request.getproxies", return_value={}):
                with self.assertRaises(ProxyUnavailable):
                    global_endpoint("unused", {"proxy_mode": "auto"})
            dial.assert_not_called()

    def test_embedded_global_password_and_encoded_login(self):
        endpoint = global_endpoint("unused", {"proxy_mode": "custom", "proxy_url": "http://u%40name:p%3Ass@127.0.0.1:8000"})
        self.assertEqual(endpoint.username, "u@name")
        self.assertEqual(endpoint.password, "p:ss")
        self.assertNotIn("p:ss", repr(endpoint))
