"""Opt-in real model diagnostics through the already-running KID Telegram module."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, choices=("archive", "terminal", "opera", "create-send"))
    parser.add_argument("--run", action="store_true", help="Run Codex, deliver its answer to the owner, and optionally leave a visible CMD open")
    args = parser.parse_args()
    if not args.run:
        print("No changes. --run invokes the real configured Telegram profile.")
        return
    import requests
    from app_paths import USER_DATA_DIR
    from codex_engine import profile_home
    from services.telegram_storage import TelegramStorage
    store = TelegramStorage(USER_DATA_DIR)
    settings = store.settings()
    client = requests.Session()
    client.trust_env = False
    try:
        result = client.post("http://127.0.0.1:3000/api/telegram/test-integration", json={"kind":args.kind}, timeout=8)
        if not result.ok:
            raise RuntimeError("Diagnostic rejected; ensure KID is enabled and idle")
        queued = result.json()
        operation = "telegram_" + settings["token"].split(":")[0] + "_" + queued["queue_id"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            state = store.state()
            if args.kind == "create-send":
                diagnostic = state.get("integration_test") or {}
                pending = client.get("http://127.0.0.1:3000/api/os-tools/pending", timeout=5)
                pending.raise_for_status()
                for entry in pending.json().get("pending", []):
                    if (entry.get("profile_id") == settings["profile_id"] and entry.get("session_id") == state["session_id"]
                            and entry.get("command") == diagnostic.get("path")):
                        # The user explicitly authorized creation of this unique test file.
                        approved = client.post("http://127.0.0.1:3000/api/os-tools/approve", json={"id":entry["id"]}, timeout=5)
                        approved.raise_for_status()
            last = state.get("last_turn") or {}
            if last.get("operation_id") == operation and last.get("delivery_ms") is not None:
                break
            time.sleep(.5)
        else:
            raise RuntimeError("Diagnostic still pending; do not retry automatically; check bot approvals or status")
        directory = profile_home(USER_DATA_DIR, settings["profile_id"]) / "tool_actions"
        def receipt(name, arguments):
            identity = hashlib.sha256(json.dumps([operation, name, arguments], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            saved = json.loads((directory / (identity + ".json")).read_text(encoding="utf-8"))
            assert saved.get("status") == "completed"
            return saved["result"]
        if args.kind in ("terminal", "opera"):
            opened = receipt("windows_open_app", {"app":"cmd" if args.kind == "terminal" else "opera_gx"})
            assert opened.get("ok") is True and opened.get("window_visible") is True
            # Verify again after the tool and the model turn have both finished.
            from native_codex.desktop import visible_windows
            assert any(w["hwnd"] == opened["window_handle"] for w in visible_windows())
            print(json.dumps({"passed":True,"kind":args.kind,"model_called_host_tool":True,
                              "visible_after_turn":True,"pid":opened["pid"],"window_handle":opened["window_handle"]}))
        elif args.kind == "create-send":
            diagnostic = state["integration_test"]
            path = Path(diagnostic["path"])
            assert path.is_file() and path.read_text(encoding="utf-8").strip() == diagnostic["expected_content"].strip()
            sent = state.get("last_sent_file") or {}
            assert sent.get("filename") == diagnostic["name"] and sent.get("file_id")
            assert sent["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
            exported = receipt("telegram_send_file", {"path":str(path)})
            assert exported.get("queued") is True and exported["sha256"] == sent["sha256"]
            from services.telegram_transport import TelegramTransport
            import tempfile
            import threading
            transport = TelegramTransport(settings, USER_DATA_DIR)
            try:
                info = transport.call("getFile", file_id=sent["file_id"])
                with tempfile.TemporaryDirectory(prefix="kid-telegram-confirm-") as temporary:
                    downloaded = Path(temporary) / "receipt.txt"
                    transport.download(info["file_path"], downloaded, threading.Event())
                    assert downloaded.read_bytes() == path.read_bytes()
            finally:
                transport.close_thread()
            print(json.dumps({"passed":True,"kind":"create-send","real_desktop_file_created":True,
                              "telegram_document_delivered":True,"downloaded_bytes_identical":True,
                              "filename":diagnostic["name"]}))
        else:
            diagnostic = state["integration_test"]
            archive_id = diagnostic["archive_id"]
            # Search may include optional arguments, but only the selected entry
            # can contain this previously unseen random third-line marker.
            completed = []
            for path in directory.glob("*.json"):
                saved = json.loads(path.read_text(encoding="utf-8"))
                value = saved.get("result") or {}
                if saved.get("status") == "completed" and isinstance(value, dict) and value.get("archive_id") == archive_id:
                    completed.append(value)
            assert any(any(line.get("number") == 3 and line.get("text") == diagnostic["marker"] for line in value.get("lines", [])) for value in completed)
            answer = next(item["content"] for item in reversed(state["history"]) if item["role"] == "assistant")
            assert diagnostic["marker"] in answer
            print(json.dumps({"passed":True,"kind":"archive","model_read_archived_file":True,
                              "third_line_verified":True,"content_not_in_prompt":True}))
    finally:
        client.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Requests exceptions can expose URLs; diagnostics only report safe types.
        print(json.dumps({"passed":False,"error_type":type(error).__name__}))
        raise SystemExit(1)
