"""Generate the installed protected binary's own schema into a scratch folder."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_paths import USER_DATA_DIR
from native_codex.process import metadata, environment
from native_codex.wfp import verify
from native_codex.broker import BROKER_PORT

metadata(USER_DATA_DIR)
executable = USER_DATA_DIR / "codex_native" / "codex.exe"
verify(executable, BROKER_PORT)
with tempfile.TemporaryDirectory(prefix="kid-native-schema-") as scratch:
    result = subprocess.run([str(executable), "app-server", "generate-json-schema", "--experimental", "--out", scratch],
        env=environment(Path(scratch) / "profile", f"http://127.0.0.1:{BROKER_PORT}"),
        capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0
    wanted = {"DynamicToolSpec", "DynamicToolCallParams", "DynamicToolCallResponse"}
    for path in Path(scratch).rglob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if path.stem in wanted:
            print(json.dumps({"type": path.stem, "schema": data}))
        if path.stem == "ThreadStartParams":
            for name, definition in data.get("definitions", {}).items():
                if "DynamicTool" in name:
                    print(json.dumps({"type": name, "schema": definition}))
