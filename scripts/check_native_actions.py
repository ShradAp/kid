"""Real model -> stdio MCP -> safe commands and files in a scratch Windows folder."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_paths import USER_DATA_DIR
from config_manager import load_config
from codex_service import CodexService
from native_codex.commands import run
from code_workspace import create_windows_file


def main():
    profile = copy.deepcopy(load_config()["profiles"]["codex"])
    calls = []
    with tempfile.TemporaryDirectory(prefix="kid-native-actions-") as folder:
        profile["codex"]["workspace"] = folder
        profile["codex"]["search"] = "kid"
        def tools(name, arguments, profile_id, cancel):
            if name == "windows_command":
                assert arguments.get("command") == "Write-Output KID-WINDOWS-OK"
                result = run(arguments["command"], "powershell", folder, cancel)
            elif name == "windows_write_file":
                assert arguments.get("file_name") == "probe.md" and arguments.get("content") == "KID-WINDOWS-OK"
                result = create_windows_file(folder, "probe.md", arguments["content"])
            else:
                raise ValueError("Only scratch Windows actions are allowed")
            assert result["ok"]
            calls.append(name)
            return result
        service = CodexService(USER_DATA_DIR, workspace_grant=lambda _: folder, local_tools=tools)
        session_id = "native-mcp-test-" + uuid.uuid4().hex
        try:
            session = service.get("codex", profile)
            requests = []
            original_request = session.server_request
            def request(method, params):
                requests.append(method)
                return original_request(method, params)
            session.server_request = request
            result = session.run(session_id,
                "Это проверка MCP на Windows. Обязательно используй windows_command с command ровно "
                "Write-Output KID-WINDOWS-OK (PowerShell). Затем используй windows_write_file с file_name probe.md, "
                "content ровно KID-WINDOWS-OK, location project. Это временная тестовая папка. Другие инструменты не используй. "
                "После фактического успеха ответь кратко.", str(uuid.uuid4()), threading.Event(), timeout=120)
            if set(calls) != {"windows_command", "windows_write_file"}:
                print(json.dumps({"calls": calls, "server_requests": requests, "reply": session.sanitize(result["reply"]),
                                  "steps": result.get("thinking", [])[-8:]}, ensure_ascii=True))
            assert set(calls) == {"windows_command", "windows_write_file"}
            assert (Path(folder) / "probe.md").read_text() == "KID-WINDOWS-OK"
            assert result["reply"]
            print(json.dumps({"native_mcp": True, "real_windows_command": True, "real_windows_file": True,
                              "search_mode": "kid", "action_results_in_steps": bool(result["thinking"])}))
        finally:
            service.forget(session_id)
            service.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"native_mcp": "failed", "error_type": type(error).__name__}))
        raise SystemExit(1)
