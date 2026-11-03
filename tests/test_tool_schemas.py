"""Tool contracts shared by API, native MCP and isolated runtime packaging."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ToolSchemaTests(unittest.TestCase):
    def test_catalogs_match_reviewed_contract(self):
        from tool_schemas import TOOLS_SCHEMA, WORD_TOOLS_SCHEMA

        for schema, expected in (
            (TOOLS_SCHEMA, "76fb64b9d4b3019c5842b8bd6224f2fa89a0ea630f6d143dfe024ed2d493a5c5"),
            (WORD_TOOLS_SCHEMA, "710615c7a5a1f452459a4fda565e841265a0664ba6c00aa97fe7c12ad6060de1"),
        ):
            # Snapshot covers descriptions, order, parameter types and required
            # fields sent to providers, including proactive fact saving and
            # memory reads without a separate user request.
            encoded = json.dumps(schema, ensure_ascii=False, sort_keys=True).encode()
            self.assertEqual(hashlib.sha256(encoded).hexdigest(), expected)

    def test_api_compatibility_names_reference_the_same_catalogs(self):
        import api_client
        import tool_schemas

        self.assertIs(api_client.TOOLS_SCHEMA, tool_schemas.TOOLS_SCHEMA)
        self.assertIs(api_client.WORD_TOOLS_SCHEMA, tool_schemas.WORD_TOOLS_SCHEMA)

    def test_schema_import_needs_neither_site_packages_nor_api_transport(self):
        root = Path(__file__).resolve().parents[1]
        code = """
import sys
sys.path.insert(0, sys.argv[1])
import tool_schemas
from native_codex.schema import tools
assert len(tool_schemas.TOOLS_SCHEMA) == 10
assert len(tools('kid')) == 13
assert len(tools('codex')) == 11
assert 'api_client' not in sys.modules
assert 'requests' not in sys.modules
"""
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", code, str(root)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_runtime_bundle_contains_schema_and_worker_imports(self):
        from codex_runtime import runtime_sources

        sources = runtime_sources()
        self.assertEqual(sources["kidlib/tool_schemas.py"],
                         (Path(__file__).resolve().parents[1] / "tool_schemas.py").read_bytes())
        for path in ("kid_tool_worker.py", "kid_mcp_server.py", "codex_worker.py"):
            compile(sources[path], path, "exec")

    def test_runtime_bundle_imports_without_host_application_or_network(self):
        from codex_runtime import runtime_sources

        with tempfile.TemporaryDirectory(prefix="kid-runtime-import-") as directory:
            root = Path(directory)
            for name, contents in runtime_sources().items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(contents)
            code = """
import pathlib, socket, sys
root=pathlib.Path(sys.argv[1])
sys.path[:0]=[str(root/'kidlib'),str(root)]
def blocked(*args,**kwargs): raise AssertionError('Import attempted network access')
socket.socket.connect=blocked
socket.socket.connect_ex=blocked
import api_client
import memory.user_profile
import services.word_automation as word
import kid_mcp_server, kid_tool_worker, codex_worker
assert pathlib.Path(api_client.__file__).is_relative_to(root/'kidlib')
assert pathlib.Path(memory.user_profile.__file__).is_relative_to(root/'kidlib')
assert word.normalize_structure([{'type':'paragraph','text':'fixture'}])[0].text=='fixture'
assert 'desktop_app' not in sys.modules
assert 'application' not in sys.modules
"""
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", code, str(root)],
                env=dict(os.environ, KID_CONFIG_DIR=str(root / "user"), HF_HUB_OFFLINE="1",
                         TRANSFORMERS_OFFLINE="1", PYTHONIOENCODING="utf-8"),
                capture_output=True, encoding="utf-8", errors="replace", timeout=20,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_search_routes_and_mutation_do_not_change_api_schema(self):
        from native_codex.schema import tools
        from tool_schemas import TOOLS_SCHEMA

        kid = tools("kid")
        codex = tools("codex")
        names = lambda items: {item["name"] for item in items}
        self.assertEqual(names(kid) - names(codex),
                         {"web_search", "analyze_github_repo"})
        self.assertIn("fetch_webpage", names(codex))
        self.assertTrue({"windows_command", "windows_write_file"} <= names(codex))
        kid[0]["inputSchema"]["properties"].clear()
        self.assertIn("query", TOOLS_SCHEMA[0]["function"]["parameters"]["properties"])
