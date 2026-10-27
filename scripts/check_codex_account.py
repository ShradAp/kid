"""Interactive ChatGPT device login and protected account smoke test.

Never launches a browser. Start only after kernel/socket isolation checks.
Prints a short-lived device code, never tokens, email or raw server errors.
"""
import argparse
import json
from pathlib import Path
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_paths import USER_DATA_DIR
from codex_service import CodexService
from config_manager import load_config


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-id", default="codex")
    parser.add_argument("--login", action="store_true")
    args = parser.parse_args()
    profile = (load_config().get("profiles") or {})[args.profile_id]
    service = CodexService(USER_DATA_DIR)
    session_id = "codex-smoke-" + uuid.uuid4().hex
    try:
        session = service.get(args.profile_id, profile)
        session.start()
        if not session.authenticated:
            if not args.login:
                print(json.dumps({"account_test": "requires_device_login"})); return
            session.login_start()
            print(json.dumps({"verification_url": session.login["verification_url"], "user_code": session.login["user_code"],
                              "browser_opened": False, "confirm_on_other_device_or_proxy_browser": True}))
            deadline = time.monotonic() + 300
            while not session.authenticated and time.monotonic() < deadline:
                if session.closed.wait(1) or session.runtime.stopped.is_set() or session.login is None:
                    session._after_login()
                    break
            if not session.authenticated:
                if session.login and session.login.get("login_id"):
                    session.rpc("account/login/cancel", {"loginId": session.login["login_id"]})
                print(json.dumps({"account_test": "login_not_completed", "inference_requests": 0})); return
        session.refresh_limits()
        marker = "KID-PROXY-OK"
        first = session.run(session_id, f"Запомни маркер {marker} для следующего сообщения. Ответь только этим маркером. Инструменты не используй.",
                            str(uuid.uuid4()), threading.Event(), timeout=90)
        assert marker in first["reply"]
        duplicate = session.run(session_id, "ignored duplicate", first["operation_id"], threading.Event())
        assert first == duplicate
        service.close()
        service = CodexService(USER_DATA_DIR)
        session = service.get(args.profile_id, profile)
        second = session.run(session_id, "Какой маркер я просил запомнить? Ответь только маркером, без инструментов.",
                             str(uuid.uuid4()), threading.Event(), timeout=90)
        assert marker in second["reply"]
        cancel = threading.Event()
        timer = threading.Timer(.5, cancel.set)
        timer.start()
        try:
            stopped = session.run(session_id, "Перечисли числа от 1 до 1000. Инструменты не используй.", str(uuid.uuid4()), cancel, timeout=30)
        finally:
            timer.cancel()
        assert stopped["cancelled"]
        print(json.dumps({"account_login_verified": True, "generation_verified": True, "completed_operation_not_replayed": True,
                          "thread_resume_after_restart_verified": True, "cancellation_verified": True,
                          "quota_windows_received": len(service.limits.read(args.profile_id)["windows"])}))
    finally:
        service.forget(session_id)
        service.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"account_test": "failed", "private_details_suppressed": True}))
        raise SystemExit(1)
