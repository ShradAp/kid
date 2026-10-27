"""Real Docker/socket integration check; no account or inference is used."""
import argparse
import json
import queue
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_paths import USER_DATA_DIR
from codex_engine import StdioConnection
from codex_runtime import IsolatedRuntime
from scripts.prepare_codex_runtime import private_endpoint


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--use-app-proxy", action="store_true")
    parser.add_argument("--profile-id")
    args = parser.parse_args()
    endpoint = private_endpoint(args.profile_id, args.use_app_proxy)
    with tempfile.TemporaryDirectory(prefix="kid-isolation-") as directory:
        runtime = IsolatedRuntime(USER_DATA_DIR, Path(directory), endpoint)
        try:
            report = runtime.start_worker()
            print(json.dumps({"kernel_isolation": report}))
            check = (
                "import json,urllib.request; "
                f"p='http://127.0.0.1:{runtime.proxy_port}'; "
                "o=urllib.request.build_opener(urllib.request.ProxyHandler({'http':p,'https':p})); "
                "r=o.open('https://api.ipify.org?format=json',timeout=25); print(json.dumps(json.load(r)))"
            )
            result = json.loads(runtime.docker("exec", runtime.container, "/usr/local/bin/python", "-I", "-c", check, timeout=40))
            print(json.dumps({"container_proxy_exit_ip": result["ip"], "upstream_credentials_in_container": False}))
            runtime.start_server("codex")
            connection = StdioConnection(runtime.reader, runtime.writer)
            connection.initialize()
            account = connection.request("account/read", {"refreshToken": False})
            assert account.get("account") is None
            models = connection.request("model/list", {}, timeout=30)
            print(json.dumps({"app_server_initialized": True, "isolated_account_is_empty": True,
                              "real_model_count": len(models.get("data", [])), "inference_requests": 0}))
            # The installed binary validates real thread configuration. There
            # is no turn/start here, so this cannot perform inference.
            arguments = {"cwd": "/home/codex", "approvalPolicy": "untrusted", "sandbox": "danger-full-access",
                         "config": {"mcp_servers": {"kid": {"command": "/usr/local/bin/python",
                                    "args": ["-I", "/opt/kid/kid_mcp_server.py", "--port", str(runtime.tool_port),
                                             "--session", "isolation-test", "--search", "codex"], "enabled": True}}}}
            thread = connection.request("thread/start", arguments)
            assert thread.get("thread", {}).get("id")
            for search in ("codex", "kid"):
                payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
                result = subprocess.run(runtime.docker_command("exec", "-i", runtime.container, "/usr/local/bin/python", "-I",
                                        "/opt/kid/kid_mcp_server.py", "--port", str(runtime.tool_port), "--session", "check", "--search", search),
                                        input=payload.encode(), capture_output=True, timeout=15,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                names = {item["name"] for item in json.loads(result.stdout)["result"]["tools"]}
                assert "save_note" in names and "create_downloadable_file" in names
                assert {"windows_command","windows_write_file"} <= names
                assert ("web_search" in names) == (search == "kid")
            print(json.dumps({"real_thread_configuration_accepted": True, "mcp_route_filter_verified": True}))
            runtime.docker("exec", runtime.container, "/opt/kid/bin/codex", "app-server", "generate-json-schema", "--out", "/profile/schema")
            schemas = list((Path(directory) / "server" / "schema").rglob("*.json"))
            supported = "\n".join(path.read_text(encoding="utf-8") for path in schemas)
            assert '"clientUserMessageId"' in supported
            assert '"chatgptDeviceCode"' in supported
            assert '"account/rateLimits/read"' in supported
            print(json.dumps({"installed_schema_verified": True}))
            connection.close()
        finally:
            runtime.close()
    # Shared document tools operate on disposable files, never user documents.
    with tempfile.TemporaryDirectory(prefix="kid-tools-check-") as directory:
        root = Path(directory)
        (root / "codex_runtime").mkdir()
        shutil.copyfile(USER_DATA_DIR / "codex_runtime" / "runtime.json", root / "codex_runtime" / "runtime.json")
        runtime = IsolatedRuntime(root, root / "worker", endpoint, network_scope="tools")
        try:
            runtime.start_worker()
            runtime.send({"kind": "execute_tool", "request": {"name": "create_downloadable_file", "arguments": {
                "file_name": "isolation-check.txt", "content": "isolated document"}}})
            frame = runtime.events.get(timeout=30)
            assert frame.get("kind") == "tool_executed" and "error" not in frame.get("result", {}), frame.get("kind")
            assert (root / "generated_files" / "index.json").is_file()
            runtime.send({"kind": "execute_tool", "request": {"name": "fetch_webpage", "arguments": {"url": "https://example.com"}}})
            frame = runtime.events.get(timeout=50)
            assert frame.get("kind") == "tool_executed" and "error" not in frame.get("result", {})
            page = json.loads(frame["result"]["result"])
            assert page.get("status") == "read" and page.get("visual_opened") is False
            print(json.dumps({"isolated_document_created": True, "proxy_only_page_text_read": True}))
        finally:
            runtime.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # All exception messages in the launcher are fixed, secret-free text.
        from codex_runtime import IsolationUnavailable
        print(str(error) if isinstance(error, IsolationUnavailable) else type(error).__name__)
        raise SystemExit(1)
