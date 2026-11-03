import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from isolated_case import IsolatedStorageTestCase


class MemoryStorageTests(IsolatedStorageTestCase):
    def setUp(self):
        from memory import user_profile

        self.memory = user_profile
        temporary = tempfile.TemporaryDirectory(prefix="kid-memory-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_json_roundtrip_similarity_and_escaped_ids(self):
        path = self.root / "facts.json"
        table = self.memory._JsonTable(path)
        table.add([{"id": "a'b", "fact": "first", "vector": [1, 0]},
                   {"id": "second", "fact": "second", "vector": [0, 1]}])
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((payload["schema_version"], payload["table"]), (1, "user_facts"))
        reopened = self.memory._JsonTable(path)
        found = reopened.search([1, 0]).metric("cosine").limit(1).to_list()
        self.assertEqual(found[0]["id"], "a'b")
        self.assertAlmostEqual(found[0]["_distance"], 0.0)
        reopened.update("id = 'a''b'", {"fact": "updated"})
        reopened.delete("id = 'second'")
        self.assertEqual(self.memory._JsonTable(path).to_arrow().to_pylist()[0]["fact"], "updated")
        reopened.reset()
        self.assertEqual(self.memory._JsonTable(path).count_rows(), 0)

    def test_hash_memory_crud_dedup_and_reload_without_ml_downloads(self):
        with patch.dict(os.environ, {"KID_MEMORY_EMBEDDER": "hash"}), \
             patch.dict("sys.modules", {"lancedb": None, "pyarrow": None}):
            store = self.memory.UserProfileMemory(db_dir=self.root / "lancedb")
            first = store.add_fact("Uses Python for local projects", "tech_stack")
            repeated = store.add_fact("Uses Python for local projects", "tech_stack")
            self.assertTrue(first["ok"])
            self.assertEqual(repeated["id"], first["id"])
            self.assertEqual(repeated["status"], "updated")
            self.assertEqual((store.backend, store.storage, store.count()), ("hash", "json", 1))
            self.assertEqual(store.search("Uses Python for local projects")[0]["id"], first["id"])
            changed = store.update_fact(first["id"], "Uses Rust for local projects", "tech_stack")
            self.assertTrue(changed["ok"])
            reopened = self.memory.UserProfileMemory(db_dir=self.root / "lancedb")
            self.assertEqual(reopened.all_facts()[0]["fact"], "Uses Rust for local projects")
            self.assertTrue(reopened.delete_fact(first["id"])["ok"])
            self.assertEqual(reopened.count(), 0)

    def test_json_migration_preserves_existing_ids_and_skips_wrong_vector_dimension(self):
        store = self.memory.UserProfileMemory(db_dir=self.root / "lancedb")
        store.dim = 2
        source = self.memory._JsonTable(store.json_path)
        source.add([{"id": "existing", "fact": "old", "vector": [1, 0]},
                    {"id": "new", "fact": "migrated", "vector": [0, 1]},
                    {"id": "wrong", "fact": "skip", "vector": [0]}])
        destination = self.memory._JsonTable(self.root / "destination.json")
        destination.add([{"id": "existing", "fact": "keep", "vector": [1, 0]}])
        self.assertEqual(store._migrate_json_rows(destination), 1)
        facts = {row["id"]: row["fact"] for row in destination.to_arrow().to_pylist()}
        self.assertEqual(facts, {"existing": "keep", "new": "migrated"})
        self.assertFalse(store.json_path.exists())
        self.assertTrue(store.json_path.with_suffix(".json.migrated").exists())

    def test_fastembed_probe_and_fallback_keep_state_without_model_downloads(self):
        model = Mock()
        model.embed.side_effect = lambda texts: ([1.0, 0.0, 0.0] for _ in texts)
        loader = Mock(return_value=model)
        fake = types.SimpleNamespace(TextEmbedding=loader)
        with patch.dict("sys.modules", {"fastembed": fake}), \
             patch.dict(os.environ, {"KID_MEMORY_EMBEDDER": "", "KID_EMBED_CACHE": str(self.root / "cache")}):
            store = self.memory.UserProfileMemory(db_dir=self.root / "lancedb")
            vectors = store._embed(["one", "two"])
            self.assertEqual((store.backend, store.dim), ("fastembed", 3))
            self.assertEqual(vectors, [[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
            self.assertTrue((self.root / "cache").is_dir())
            loader.side_effect = RuntimeError("fixture model unavailable")
            fallback = self.memory.UserProfileMemory(db_dir=self.root / "fallback")
            vector = fallback._embed(["one"])[0]
            self.assertEqual((fallback.backend, fallback.dim, len(vector)), ("hash", 384, 384))
            self.assertIn("fixture model unavailable", fallback.last_error)

    def test_forced_hash_never_initializes_fastembed(self):
        loader = Mock(side_effect=AssertionError("FastEmbed must not initialize"))
        with patch.dict("sys.modules", {"fastembed": types.SimpleNamespace(TextEmbedding=loader)}), \
             patch.dict(os.environ, {"KID_MEMORY_EMBEDDER": "hash"}):
            store = self.memory.UserProfileMemory(db_dir=self.root / "lancedb")
            vector = store._embed(["Python project"])[0]
        self.assertEqual(vector, self.memory._HashEmbedder().embed_one("Python project"))
        self.assertAlmostEqual(sum(value * value for value in vector), 1.0)
        loader.assert_not_called()

    def test_native_memory_tools_save_list_update_delete_and_inject_phone(self):
        from native_codex.tools import execute
        store = self.memory.UserProfileMemory(db_dir=self.root / "lancedb")
        with patch.object(self.memory, "get_store", return_value=store), \
             patch.dict(os.environ, {"KID_MEMORY_EMBEDDER": "hash"}), \
             patch.dict("sys.modules", {"lancedb": None, "pyarrow": None}):
            def call(name, **arguments):
                return execute(name, arguments, None, None)
            saved = call("remember_user_fact", fact="Пользователь использует Samsung Galaxy S25 Ultra", category="hardware")
            self.assertTrue(saved["ok"])
            listed = call("manage_memory", action="list")
            self.assertEqual(listed["facts"][0]["id"], saved["id"])
            self.assertIn("Samsung Galaxy S25 Ultra", self.memory.build_memory_context("Какой у меня телефон?"))
            updated = call("manage_memory", action="update", fact_id=saved["id"], fact="Пользователь использует другой телефон", category="hardware")
            self.assertTrue(updated["ok"])
            self.assertTrue(call("manage_memory", action="delete", fact_id=saved["id"])["ok"])
            self.assertEqual(call("manage_memory", action="list")["facts"], [])
            self.assertTrue(call("manage_memory", action="clear")["ok"])
