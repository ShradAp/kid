"""Generated tool images survive protocol conversion, persistence and reload."""
import base64
import io
import tempfile
from pathlib import Path
import unittest
from PIL import Image

from codex_images import collect_images, image_file


class GeneratedImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        stream = io.BytesIO()
        Image.new("RGB", (12, 8), "orange").save(stream, format="PNG")
        self.data = stream.getvalue()
        self.encoded = base64.b64encode(self.data).decode()
        self.url = "data:image/png;base64," + self.encoded

    def test_generation_and_code_mode_outputs_share_one_durable_file(self):
        first = collect_images(self.root, {"type": "imageGeneration", "status": "completed", "result": self.encoded})
        second = collect_images(self.root, {"type": "functionCallOutput", "output": [
            {"type": "input_text", "text": "generated"}, {"type": "input_image", "image_url": self.url}]})
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        path = image_file(self.root, first[0]["url"].rsplit("/", 1)[1])
        self.assertEqual(path.read_bytes(), self.data)
        self.assertNotIn(self.encoded, str(first))

    def test_saved_path_is_limited_to_generated_or_granted_workspace(self):
        generated = self.root / "native_server" / "generated_images"
        generated.mkdir(parents=True)
        source = generated / "test.png"
        source.write_bytes(self.data)
        item = {"type": "imageGeneration", "status": "completed", "savedPath": str(source), "fileId": "file-one"}
        self.assertEqual(collect_images(self.root, item), [])
        self.assertEqual(len(collect_images(self.root, item, [generated])), 1)
        self.assertIsNone(image_file(self.root, "../native_server/generated_images/test.png"))

    def test_references_remote_urls_and_invalid_images_are_not_exposed(self):
        self.assertEqual(collect_images(self.root, {"type": "userMessage", "content": [
            {"type": "input_image", "image_url": self.url}]}), [])
        for url in ("https://example.com/private.png", "data:image/png;base64,bad=", "data:image/svg+xml;base64,PHN2Zz4="):
            self.assertEqual(collect_images(self.root, {"type": "functionCallOutput", "output": [
                {"type": "input_image", "image_url": url}]}), [])


if __name__ == "__main__":
    unittest.main()
