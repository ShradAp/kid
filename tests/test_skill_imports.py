"""Explicit model installs copy verified skill files and never enable a profile."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from application.codex_actions import execute_host_tool
from services import skill_imports
from tool_execution import ToolExecutionError

SHA = "a" * 40
SKILL = b"---\nname: example\ndescription: Read PDF documents\n---\nFULL_INSTRUCTIONS\n"


class SkillImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.cfg = {"profiles": {"a": {"engine": "codex", "extension_ids": []}, "b": {"engine": "codex", "extension_ids": []}}}
        self.save(self.cfg)
        self.folder = self.root / "example"
        self.folder.mkdir()
        (self.folder / "SKILL.md").write_bytes(SKILL)

    def save(self, value):
        self.cfg = copy.deepcopy(value)
        self.data.mkdir(exist_ok=True)
        (self.data / "config.json").write_text(json.dumps(self.cfg), encoding="utf-8")

    def call(self, **kwargs):
        return skill_imports.install_skill(self.data, lambda: copy.deepcopy(self.cfg), self.save, **kwargs)

    def tree(self, extras=None):
        return {"tree": [{"path": "skills/example/SKILL.md", "type": "blob", "mode": "100644", "size": len(SKILL)}, *(extras or [])]}

    def remote(self, tree=None):
        def metadata(url):
            return {"sha": SHA} if "/commits/" in url else (tree or self.tree())
        return patch.object(skill_imports, "_json", side_effect=metadata)

    def test_local_model_tool_inspects_then_installs_without_enabling_or_running_scripts(self):
        (self.folder / "install.py").write_text("raise RuntimeError('must not run')", encoding="utf-8")
        cancel = threading.Event()
        session = types.SimpleNamespace(runtime=types.SimpleNamespace(native=True), lock=threading.RLock(),
                                        active={"thread": {"session_id": "chat", "cancel": cancel}})
        service = types.SimpleNamespace(directory=self.data, sessions={"a": ("saved", session)})
        def tool(arguments):
            return execute_host_tool("install_skill", arguments, "a", cancel, service=service, workspaces=Mock(),
                load_config=lambda: copy.deepcopy(self.cfg), save_config=self.save, read_notes=Mock(), prepend_note=Mock())
        with patch("subprocess.Popen") as process:
            preview = tool({"action": "inspect", "source": str(self.folder)})
            self.assertFalse(self.cfg.get("extensions"))
            self.assertEqual(set(preview["files"]), {"SKILL.md", "install.py"})
            result = tool({"action": "install", "source": str(self.folder), "fingerprint": preview["fingerprint"]})
        process.assert_not_called()
        self.assertTrue(result["installed"])
        self.assertFalse(result["profile_selection_changed"])
        self.assertTrue(all(not profile["extension_ids"] for profile in self.cfg["profiles"].values()))
        self.assertEqual(Path(self.cfg["extensions"][0]["skills"][0]["path"]).read_bytes(), SKILL)

    def test_github_download_is_pinned_and_contains_resources_and_provenance(self):
        extra = {"path": "skills/example/reference.md", "type": "blob", "mode": "100644", "size": 8}
        source = "https://skills.sh/acme/repository/example"
        def contents(url, limit):
            self.assertIn(SHA, url)
            return SKILL if url.endswith("SKILL.md") else b"resource"
        with self.remote(self.tree([extra])), patch.object(skill_imports, "_download", side_effect=contents):
            preview = self.call(action="inspect", source=source)
        self.assertIn(SHA, preview["source"])
        self.assertEqual(set(preview["files"]), {"SKILL.md", "reference.md"})
        result = self.call(action="install", source=source, fingerprint=preview["fingerprint"])
        self.assertTrue(result["installed"])
        item = self.cfg["extensions"][0]
        self.assertEqual(item["provenance"]["commit"], SHA)
        self.assertEqual(item["version"], SHA[:12])
        self.assertEqual((Path(item["skills"][0]["path"]).parent / "reference.md").read_text(), "resource")

    def test_no_install_without_inspection_or_after_source_change(self):
        with self.assertRaisesRegex(ValueError, "Сначала"):
            self.call(action="install", source=str(self.folder))
        preview = self.call(action="inspect", source=str(self.folder))
        (self.folder / "SKILL.md").write_bytes(SKILL + b"changed")
        with self.assertRaisesRegex(ValueError, "изменился"):
            self.call(action="install", source=str(self.folder), fingerprint=preview["fingerprint"])
        self.assertFalse(self.cfg.get("extensions"))

    def test_duplicate_remote_import_does_not_create_another_entry(self):
        source = "acme/repository@example"
        with self.remote(), patch.object(skill_imports, "_download", return_value=SKILL):
            preview = self.call(action="inspect", source=source)
            first = self.call(action="install", source=source, fingerprint=preview["fingerprint"])
            second = self.call(action="inspect", source=source)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["already_installed"])
        self.assertEqual(len(self.cfg["extensions"]), 1)

    def test_unapproved_hosts_and_traversal_are_rejected_before_download(self):
        with patch.object(skill_imports, "_download") as download:
            for source in ("http://github.com/acme/repo", "https://127.0.0.1/secret", "https://evil.test/repo",
                           "https://github.com/acme/repo/tree/main/../secret", "https://user:token@github.com/acme/repo"):
                with self.assertRaises(ValueError):
                    self.call(action="inspect", source=source)
            download.assert_not_called()

    def test_symlinks_missing_instruction_and_ambiguous_repository_fail(self):
        bad_link = {"path": "skills/example/link", "type": "blob", "mode": "120000", "size": 1}
        other = {"path": "skills/another/SKILL.md", "type": "blob", "mode": "100644", "size": 1}
        for source, tree in (("https://skills.sh/acme/repo/example", self.tree([bad_link])),
                             ("https://github.com/acme/repo", self.tree([other])),
                             ("https://skills.sh/acme/repo/missing", self.tree())):
            with self.remote(tree), patch.object(skill_imports, "_download") as download:
                with self.assertRaises(ValueError):
                    self.call(action="inspect", source=source)
                download.assert_not_called()

    def test_cancelled_import_never_mutates_library(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(ValueError, "отменено"):
            self.call(action="inspect", source=str(self.folder), cancel=cancel)
        self.assertFalse(self.cfg.get("extensions"))

    def test_host_tool_requires_active_turn(self):
        service = types.SimpleNamespace(directory=self.data, sessions={})
        with self.assertRaisesRegex(ValueError, "активного"):
            execute_host_tool("install_skill", {"action": "inspect", "source": str(self.folder)}, "a", threading.Event(),
                service=service, workspaces=Mock(), load_config=lambda: copy.deepcopy(self.cfg), save_config=self.save,
                read_notes=Mock(), prepend_note=Mock())

    def test_native_catalog_advertises_install_tool_with_explicit_source_review(self):
        from native_codex.schema import tools
        tool = next(item for item in tools("codex") if item["name"] == "install_skill")
        self.assertIn("explicitly asks", tool["description"])
        self.assertEqual(tool["inputSchema"]["properties"]["action"]["enum"], ["inspect", "install"])


if __name__ == "__main__":
    unittest.main()
