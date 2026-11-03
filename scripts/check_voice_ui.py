"""Exercise voice controls against an isolated backend; no microphone or account."""
import http.client
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from playwright.sync_api import sync_playwright, expect
    with tempfile.TemporaryDirectory(prefix="kid-voice-ui-") as directory:
        os.environ["KID_CONFIG_DIR"] = directory
        Path(directory, "config.json").write_text(json.dumps({
            "active_profile": "main",
            "profiles": {"main": {"engine": "api", "api_key": "", "model": "fixture"}},
        }), encoding="utf-8")
        import desktop_app as app
        from services import voice_input
        app.get_live_balance = lambda *args, **kwargs: None
        app.user_profile_memory = None
        voice = voice_input.VoiceInput(directory)
        voice_input._instance = voice
        marker = voice.model_path / "am" / "final.mdl"
        marker.parent.mkdir(parents=True)
        marker.touch()
        def fake_microphone():
            with voice.lock:
                voice.state = "waiting"
            voice.stop_event.wait(30)
        voice._run = fake_microphone
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), app.BridgeHandler)
        app.BRIDGE_PORT = server.server_port
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            frontend_port = reservation.getsockname()[1]
        origin = f"http://127.0.0.1:{frontend_port}"
        environment = dict(os.environ, KID_BRIDGE_TOKEN=app.BRIDGE_TOKEN,
                           KID_BRIDGE_PORT=str(server.server_port), NEXT_TELEMETRY_DISABLED="1")
        with tempfile.TemporaryFile() as log:
            frontend = subprocess.Popen(
                ["node", "node_modules/next/dist/bin/next", "start", "--hostname",
                 "127.0.0.1", "--port", str(frontend_port)],
                cwd=ROOT / "UI", env=environment, stdout=log, stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    try:
                        local.open(origin, timeout=1).close()
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError("Test frontend did not start")
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        context = browser.new_context(viewport={"width": 1200, "height": 900})
                        sent = []
                        def route_local(route):
                            if not route.request.url.startswith(origin + "/"):
                                route.abort()
                                return
                            url = urlsplit(route.request.url)
                            if not url.path.startswith("/api/"):
                                route.continue_()
                                return
                            if url.path == "/api/chat":
                                sent.append(True)
                                route.abort()
                                return
                            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                            try:
                                headers = {"X-Kid-Token": app.BRIDGE_TOKEN}
                                if route.request.headers.get("content-type"):
                                    headers["Content-Type"] = route.request.headers["content-type"]
                                connection.request(route.request.method, url.path + ("?" + url.query if url.query else ""),
                                                   body=route.request.post_data_buffer, headers=headers)
                                response = connection.getresponse()
                                route.fulfill(status=response.status,
                                              content_type=response.getheader("Content-Type", "application/json"),
                                              body=response.read())
                            finally:
                                connection.close()
                        context.route("**/*", route_local)
                        page = context.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(origin)
                        draft = page.get_by_placeholder("Message your assistant...")
                        expect(draft).to_be_visible()
                        draft.fill("ручной текст")
                        page.get_by_role("button", name="Включить фоновое прослушивание", exact=True).click()
                        expect(page.get_by_text("Жду «начать запись». Микрофон слушает локально.", exact=True)).to_be_visible(timeout=10000)
                        voice.consume_text("обычная фоновая речь")
                        expect(draft).to_have_value("ручной текст")
                        voice.consume_text("начать запись привет мир")
                        expect(draft).to_have_value("ручной текст привет мир", timeout=10000)
                        voice.consume_text("ещё текст конец записи не записывать")
                        expect(draft).to_have_value("ручной текст привет мир еще текст")
                        expect(page.get_by_text("Жду «начать запись». Микрофон слушает локально.", exact=True)).to_be_visible()
                        # Additional polls must not replay previously delivered events.
                        page.wait_for_timeout(1200)
                        expect(draft).to_have_value("ручной текст привет мир еще текст")
                        page.get_by_role("button", name="Выключить фоновое прослушивание", exact=True).click()
                        expect(page.get_by_role("button", name="Включить фоновое прослушивание", exact=True)).to_be_enabled()
                        assert not voice.enabled and not voice.thread.is_alive()
                        assert not sent, "Dictation must never auto-send"
                        assert not errors, errors
                        page.goto("about:blank")
                        context.unroute_all(behavior="wait")
                    finally:
                        browser.close()
                print("Voice UI passed: toggle, commands, draft append, no replay, microphone stop, no auto-send.")
            finally:
                voice.close()
                frontend.terminate()
                try:
                    frontend.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    frontend.kill()
                    frontend.wait(timeout=5)
                server.shutdown()
                server.server_close()
                if sys.exc_info()[0] is not None:
                    log.seek(0)
                    print(log.read(5000).decode("utf-8", errors="replace"))


if __name__ == "__main__":
    main()
