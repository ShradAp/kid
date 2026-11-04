"""Offline chat flow regression against the built UI; no account or Codex launch."""
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def main():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryFile() as log:
        frontend = subprocess.Popen(
            ["node", "node_modules/next/dist/bin/next", "start", "--hostname", "127.0.0.1", "--port", str(port)],
            cwd=ROOT / "UI", stdout=log, stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(100):
                if frontend.poll() is not None:
                    raise RuntimeError("Frontend exited before startup")
                try:
                    local.open(origin, timeout=1).close()
                    break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError("Frontend did not start")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1200, "height": 950})
                errors, calls, pending = [], [], {}
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script("""globalThis.chatSounds = 0;
                    Object.defineProperty(document, 'hasFocus', {value: () => true});
                    globalThis.AudioContext = class {
                      state='running'; currentTime=0; destination={};
                      resume(){return Promise.resolve()}
                      createOscillator(){return {connect(){},disconnect(){},frequency:{setValueAtTime(){}},
                        start(){globalThis.chatSounds++},stop(){}}}
                      createGain(){return {connect(){},disconnect(){},gain:{setValueAtTime(){},
                        linearRampToValueAtTime(){},exponentialRampToValueAtTime(){}}}}
                    };""")

                def route_request(route):
                    if not route.request.url.startswith(origin + "/"):
                        route.abort()  # All browser egress is blocked.
                        return
                    path = urlsplit(route.request.url).path
                    if not path.startswith("/api/"):
                        route.continue_()
                        return
                    body = route.request.post_data_json or {}
                    calls.append((path, body))
                    if path == "/api/chat":
                        assert body["session_id"] not in pending, "Overlapping turns in the same chat"
                        pending[body["session_id"]] = route
                        return
                    if path == "/api/init":
                        data = {"personas": [{"id": "p", "name": "Test", "engine": "codex"}],
                            "active_profile": "p", "active_session": "first",
                            "sessions": [{"id": sid, "title": sid, "personaId": "p", "timestamp": "Now"}
                                         for sid in ("first", "second")], "session_messages": {}}
                    elif path == "/api/notes":
                        data = {"notes": []}
                    else:
                        data = {"ok": True, "pending": [], "events": [], "models": [],
                                "active": True, "steps": [], "available": True, "limits": {"windows": []}}
                    route.fulfill(json=data)

                page.route("**/*", route_request)
                page.goto(origin)
                expect(page.get_by_role("button", name="Лимиты и состояние Codex")).to_be_visible()
                draft = page.get_by_placeholder("Message your assistant...")

                def submit(text):
                    draft.fill(text)
                    page.get_by_role("button", name="Отправить сообщение", exact=True).click()
                    expect(page.get_by_role("button", name="Уточнить задачу", exact=True)).to_be_visible()

                def select_chat(sid, working=False):
                    page.get_by_role("button", name="Open menu", exact=True).click()
                    if working:
                        expect(page.get_by_label("Модель работает").first).to_be_visible()
                    page.get_by_role("button", name=sid + " Now", exact=True).click()
                    expect(page.get_by_role("dialog", name="Assistant menu")).not_to_be_visible()

                def complete(sid, status=200):
                    route = pending.pop(sid)
                    data = {"reply": "Completed " + sid, "activity": {"status": "completed"}} if status == 200 else {"error": "Fixture failure"}
                    route.fulfill(status=status, json=data)

                submit("Initial task")
                draft.fill("Clarification")
                draft.press("Enter")
                expect(page.get_by_text("Clarification", exact=True)).to_be_visible()
                for text in ("Queued one", "Queued two"):
                    draft.fill(text)
                    draft.press("Alt+Enter")
                expect(page.get_by_text("В очереди: 2", exact=True)).to_be_visible()
                assert len([item for item in calls if item[0] == "/api/chat/steer"]) == 1
                assert not any(path == "/api/stop" for path, _ in calls)
                select_chat("second", working=True)
                complete("first")
                page.wait_for_function("JSON.parse(localStorage.getItem('kid-unread-chats') || '{}').first === true")
                select_chat("first")
                expect(page.get_by_text("В очереди: 1", exact=True)).to_be_visible()
                page.wait_for_function("!JSON.parse(localStorage.getItem('kid-unread-chats') || '{}').first")
                complete("first")
                expect(page.get_by_text("Queued two", exact=True)).to_be_visible()
                complete("first")
                expect(page.get_by_role("button", name="Отправить сообщение", exact=True)).to_be_visible()
                assert page.evaluate("globalThis.chatSounds") == 3
                prompts = [body["prompt"] for path, body in calls if path == "/api/chat"]
                assert prompts == ["Initial task", "Queued one", "Queued two"], prompts
                submit("Will fail")
                draft.fill("Resume explicitly")
                page.get_by_role("button", name="В очередь", exact=True).click()
                complete("first", 503)
                expect(page.get_by_role("button", name="Продолжить очередь", exact=True)).to_be_visible()
                assert not pending
                assert page.evaluate("globalThis.chatSounds") == 3
                page.get_by_role("button", name="Продолжить очередь", exact=True).click()
                expect(page.get_by_text("Resume explicitly", exact=True)).to_be_visible()
                draft.fill("Cancelled pending request")
                page.get_by_role("button", name="В очередь", exact=True).click()
                page.get_by_role("button", name="Stop generating", exact=True).click()
                expect(page.get_by_text("⏹ Генерация остановлена.", exact=True)).to_be_visible()
                # The browser aborts its request; release the fixture as well.
                pending.pop("first").abort()
                expect(page.get_by_label("Очередь запросов", exact=True)).to_have_count(0)
                assert len([item for item in calls if item[0] == "/api/chat"]) == 5
                assert not errors, errors
                browser.close()
                print("Chat UI checks passed: steer, FIFO, background status, unread, sound, error pause and stop.")
        finally:
            frontend.terminate()
            frontend.wait(timeout=10)


if __name__ == "__main__":
    main()
