"""Protected native handshake/account check; never bypasses the WFP gate."""
import json
from pathlib import Path
import sys
import subprocess
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_paths import USER_DATA_DIR
from codex_engine import StdioConnection, profile_home
from native_codex.process import ProtectedProcess
from prepare_codex_runtime import private_endpoint


def main():
    runtime = ProtectedProcess(USER_DATA_DIR, profile_home(USER_DATA_DIR, "codex"), private_endpoint("codex"))
    connection = None
    process = None
    stage = "gate"
    popen = subprocess.Popen
    try:
        def capture(*args, **kwargs):
            kwargs["stderr"] = subprocess.PIPE
            return popen(*args, **kwargs)
        with patch("native_codex.process.subprocess.Popen", side_effect=capture):
            process = runtime.start("codex")
        stage = "initialize"
        connection = StdioConnection(process.stdout, process.stdin)
        connection.initialize()
        stage = "account"
        account = connection.request("account/read", {"refreshToken": False}).get("account")
        models = connection.request("model/list").get("data", [])
        stage = "limits"
        limits = connection.request("account/rateLimits/read") if account else {}
        print(json.dumps({"native_handshake": True, "saved_login": bool(account), "models": len(models),
                          "limits_received": bool(limits), "enforcement": runtime.report, "generation_requests": 0}))
    except Exception:
        runtime.close()
        from codex_service import redact
        private = [runtime.endpoint.password, runtime.endpoint.username]
        try:
            auth = json.loads((runtime.home / "native_server" / "auth.json").read_text(encoding="utf-8"))
            private.extend(value for value in (auth.get("tokens") or {}).values() if isinstance(value, str))
        except (OSError, ValueError):
            pass
        diagnostic = process.stderr.read(4000) if process and process.stderr else ""
        print(json.dumps({"stage": stage, "exit": process.returncode if process else None,
                          "diagnostic": redact(diagnostic, private)}, ensure_ascii=True))
        raise
    finally:
        if connection:
            connection.close()
        runtime.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"native_check": "failed", "error_type": type(error).__name__}))
        raise SystemExit(1)
