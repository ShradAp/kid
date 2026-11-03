"""Backup fixtures use only disposable project and user-data directories."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from services.backups import create_project_backup
from isolated_case import IsolatedStorageTestCase


class BackupTests(IsolatedStorageTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="kid-backup-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / "project"
        self.user = self.root / "user"
        self.backups = self.user / ".backups"
        self.project.mkdir()
        self.user.mkdir()
        (self.user / "history").mkdir()
        (self.user / "config_snapshots").mkdir()
        (self.user / "config.json").write_text(json.dumps({"profiles": {}, "active_profile": "main"}))

    def backup(self):
        with patch("config_manager.CONFIG_PATH", self.user / "config.json"), \
             patch("config_manager.CHAT_HISTORY_DIR", self.user / "history"), \
             patch("config_manager.CONFIG_SNAPSHOT_DIR", self.user / "config_snapshots"):
            return create_project_backup(self.project, self.backups, self.user, lambda message: None)

    def test_archive_layout_exclusions_and_read_only_restore_plan(self):
        (self.project / "entry.py").write_text("print('example')")
        for name in (".env", "debug.log", "pending.tmp"):
            (self.project / name).write_text("excluded")
        for name in (".git", ".venv", ".next", "node_modules"):
            (self.project / name).mkdir()
            (self.project / name / "excluded.txt").write_text("excluded")
        (self.user / "history" / "one.json").write_text("[]")
        (self.user / "notes.json").write_text('[{"content":"example"}]')
        (self.user / ".env").write_text("EXAMPLE=fixture")
        (self.user / "telegram.json").write_text('{"token":"fixture"}')
        (self.user / "telegram" / "history").mkdir(parents=True)
        (self.user / "telegram" / "history" / "chat.json").write_text('{"history":[]}')
        (self.user / "extensions" / "fixture").mkdir(parents=True)
        (self.user / "extensions" / "fixture" / "SKILL.md").write_text("fixture skill")
        for number in range(7):
            (self.user / "config_snapshots" / f"{number}.json").write_text("{}")
        name, count = self.backup()
        self.assertEqual(count, 1)
        archive = self.backups / name
        with zipfile.ZipFile(archive) as stream:
            names = set(stream.namelist())
            self.assertTrue({"entry.py", "config.json", "_history/one.json", "_user_data/history/one.json",
                             "_user_data/notes.json", "_user_data/.env", "_user_data/telegram.json",
                             "_user_data/telegram/history/chat.json"} <= names)
            self.assertIn("_user_data/extensions/fixture/SKILL.md", names)
            self.assertEqual({n for n in names if n.startswith("_config_snapshots/")},
                             {f"_config_snapshots/{number}.json" for number in range(2, 7)})
            self.assertFalse({".env", "debug.log", "pending.tmp"} & names)
            self.assertFalse(any(n.startswith((".git/", ".venv/", ".next/", "node_modules/")) for n in names))
        restore_user = self.root / "restore-preview"
        result = subprocess.run(
            [sys.executable, "-B", "restore_backup.py", "--archive", str(archive)],
            cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ, KID_CONFIG_DIR=str(restore_user)),
            capture_output=True, encoding="utf-8", errors="replace", timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((restore_user / "config.json").exists())
        self.assertFalse((restore_user / "notes.json").exists())
        restored = subprocess.run(
            [sys.executable, "-B", "restore_backup.py", "--archive", str(archive), "--apply"],
            cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ, KID_CONFIG_DIR=str(restore_user)),
            capture_output=True, encoding="utf-8", errors="replace", timeout=15,
        )
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(json.loads((restore_user / "telegram.json").read_text()), {"token": "fixture"})
        self.assertEqual(json.loads((restore_user / "telegram" / "history" / "chat.json").read_text()), {"history": []})
        self.assertEqual((restore_user / "extensions" / "fixture" / "SKILL.md").read_text(), "fixture skill")

    def test_rotation_keeps_ten_newest_archives(self):
        self.backups.mkdir()
        for number in range(11):
            archive = self.backups / f"backup_{number:03}.zip"
            archive.write_bytes(b"fixture")
            os.utime(archive, (number + 1, number + 1))
        _, count = self.backup()
        self.assertEqual(count, 10)
        self.assertEqual(len(list(self.backups.glob("backup_*.zip"))), 10)
        self.assertFalse((self.backups / "backup_000.zip").exists())
        self.assertFalse((self.backups / "backup_001.zip").exists())
