"""Natural request -> production MCP -> approval -> real Windows Desktop file."""
import copy
import json
from pathlib import Path
import sys
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import desktop_app as app
    from code_workspace import windows_desktop
    cfg = app.load_config()
    profile_id = next(key for key, value in cfg["profiles"].items() if value.get("engine") == "codex")
    profile = copy.deepcopy(cfg["profiles"][profile_id])
    profile["codex"].update(workspace="", search="kid")
    name = "kid_file_check_" + uuid.uuid4().hex + ".py"
    target = windows_desktop() / name
    content = "print('KID-FILE-OK')"
    service = app.CODEX_SERVICE
    calls, approved = [], []
    production = service.local_tools
    def execute(tool, arguments, pid, cancel):
        assert tool == "windows_write_file" and arguments.get("file_name") == name
        assert arguments.get("location") == "desktop" and arguments.get("content", "").strip() == content
        calls.append(tool)
        return production(tool, arguments, pid, cancel)
    service.local_tools = execute
    chat = "file-write-check-" + uuid.uuid4().hex
    stop = threading.Event()
    def reviewer():
        while not stop.wait(.05):
            for item in service.approvals.pending():
                if item.get("session_id") != chat:
                    continue
                permitted = item.get("profile_id") == profile_id and item.get("command") == str(target)
                service.approvals.decide(item["id"], permitted)
                if permitted:
                    approved.append(True)
    worker = threading.Thread(target=reviewer, daemon=True)
    worker.start()
    try:
        session = service.get(profile_id, profile)
        result = session.run(chat,
            f"Создай на моём рабочем столе файл {name}, содержащий ровно {content}. Не запускай его.",
            str(uuid.uuid4()), threading.Event(), timeout=120)
        assert calls and approved, "Expected real host file tool and approval"
        assert target.read_text(encoding="utf-8").strip() == content
        assert result["reply"]
        print(json.dumps({"natural_file_request": True, "real_windows_desktop": True,
                          "python_file_created": True, "approval_verified": True, "strict_proxy": True}))
    finally:
        stop.set()
        worker.join(timeout=1)
        service.forget(chat)
        service.close()
        service.local_tools = production
        # Only the uniquely named file created by this check may be removed.
        if calls and target.is_file() and target.read_text(encoding="utf-8").strip() == content:
            target.unlink()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"natural_file_request": False, "error_type": type(error).__name__}))
        raise SystemExit(1)
