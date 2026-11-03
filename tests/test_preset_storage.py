import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from isolated_case import IsolatedStorageTestCase


class PresetStorageTests(IsolatedStorageTestCase):
    def setUp(self):
        from services.word_automation import preset_schema

        self.schema = preset_schema
        temporary = tempfile.TemporaryDirectory(prefix="kid-presets-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"KID_WORD_PRESETS_DIR": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_named_path_dict_inline_and_instance_sources_preserve_schema(self):
        preset = self.schema.default_gost_preset()
        original_name = preset.name
        path = self.schema.save_preset(preset, "renamed")
        self.assertEqual(preset.name, original_name)
        expected = preset.clone(name="renamed").model_dump(mode="json")
        for source in ("renamed", path, Path(path), expected,
                       self.schema.load_preset(path).to_json()):
            self.assertEqual(self.schema.load_preset(source).model_dump(mode="json"), expected)
        self.assertIs(self.schema.load_preset(preset), preset)
        self.assertEqual(self.schema.list_presets(), ["renamed"])
        self.assertEqual(Path(path).parent, self.root)

    def test_failed_atomic_replace_preserves_file_and_removes_temporary(self):
        preset = self.schema.default_gost_preset()
        path = Path(self.schema.save_preset(preset, "fixture"))
        original = path.read_bytes()
        with patch.object(self.schema.os, "replace", side_effect=OSError("fixture write failed")):
            with self.assertRaises(self.schema.PresetError):
                self.schema.save_preset(preset.clone(description="changed"), "fixture")
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_default_creation_and_invalid_file_errors_remain_compatible(self):
        default = self.schema.load_preset(None)
        self.assertEqual(default.name, self.schema.DEFAULT_PRESET_NAME)
        self.assertTrue(self.schema.preset_path(default.name).is_file())
        broken = self.root / "broken.json"
        broken.write_text("{", encoding="utf-8")
        with self.assertRaises(self.schema.PresetError):
            self.schema.load_preset(broken)
        with self.assertRaises(self.schema.PresetNotFoundError):
            self.schema.load_preset("missing")
