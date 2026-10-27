"""Real proxy-only GitHub/search requests with an explicit fake model callback.

Checks tool networking and controller plumbing, not model answer quality.
Does not require ChatGPT login or send paid model API requests.
"""
import json
from pathlib import Path
import queue
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_paths import USER_DATA_DIR
from codex_runtime import IsolatedRuntime, IsolationUnavailable
from scripts.prepare_codex_runtime import private_endpoint


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)
    cfg = json.loads((USER_DATA_DIR / "config.json").read_text(encoding="utf-8"))
    endpoint = private_endpoint(use_app_proxy=True)
    model_calls = []
    def fake_model(frame):
        assert frame.get("kind") == "model_call"
        messages = frame.get("request", {}).get("messages", [])
        model_calls.append(messages)
        # Finish is accepted by the existing controller only with actual read
        # evidence. It cannot fabricate a successful page read from a snippet.
        text = str(messages[-1].get("content", ""))
        try:
            card = json.loads(text)
            evidence = card.get("untrusted_evidence", [])
            urls = [item["url"] for item in evidence if item.get("read_status") == "read"]
            return {"text": json.dumps({"decision": "finish", "evidence_urls": urls,
                "questions": [], "contradictions": [], "findings": [{"claim": "Smoke evidence read", "urls": urls}],
                "assessment": "Test model callback; no real model was called"})}
        except ValueError:
            assert "octocat/Hello-World" in text and "README" in text
            return {"text": "github-metadata-smoke-ok"}

    with tempfile.TemporaryDirectory(prefix="kid-tools-network-") as directory:
        runtime = IsolatedRuntime(USER_DATA_DIR, Path(directory), endpoint, network_scope="tools", on_tool=fake_model)
        try:
            runtime.start_worker()
            def execute(name, arguments):
                runtime.send({"kind": "execute_tool", "request": {"session_id": "smoke", "name": name,
                    "arguments": arguments, "search_api_key": cfg.get("search_api_key", ""), "research_mode": "adaptive"}})
                deadline = time.monotonic() + 175
                while time.monotonic() < deadline:
                    try:
                        frame = runtime.events.get(timeout=.2)
                    except queue.Empty:
                        continue
                    if frame.get("kind") == "tool_executed":
                        result = frame.get("result", {})
                        assert not result.get("error"), "isolated tool returned an error"
                        return result.get("result")
                    if frame.get("kind") == "fatal":
                        raise IsolationUnavailable("Worker stopped during tool networking test")
                raise AssertionError("Tool budget exceeded")
            result = execute("analyze_github_repo", {"url": "https://github.com/octocat/Hello-World"})
            assert result == "github-metadata-smoke-ok"
            print(json.dumps({"github_metadata_through_proxy": True, "fake_model_callback_verified": True, "real_model_requests": 0}))
            if cfg.get("search_api_key"):
                before = len(model_calls)
                result = execute("web_search", {"query": "Python official documentation tutorial", "sources": []})
                assert "https://" in result, "search returned no source URLs"
                assert len(model_calls) > before, "research controller callback was not exercised"
                print(json.dumps({"serper_search_through_proxy": True, "research_controller_callback_verified": True, "real_model_requests": 0}))
            else:
                print(json.dumps({"serper_search": "not_configured", "real_model_requests": 0}))
        finally:
            runtime.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, (IsolationUnavailable, AssertionError)) else "Isolated tool smoke failed")
        raise SystemExit(1)
