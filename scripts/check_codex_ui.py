"""Local UI smoke test with disposable profiles and blocked browser egress.

Run after `npm run build` in UI. Requires Playwright's Chromium. Uses temporary
ports; browser API routes are forwarded to the authenticated test bridge.
Does not launch Codex or use an account, API key, or external service.
"""
import json
import http.client
import os
import socket
from pathlib import Path
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
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    from playwright.sync_api import sync_playwright, expect
    with tempfile.TemporaryDirectory(prefix="kid-codex-ui-") as directory:
        os.environ["KID_CONFIG_DIR"] = directory
        Path(directory, "config.json").write_text(json.dumps({
            "active_profile": "main", "profiles": {"main": {
                "engine": "codex", "display_name": "Codex test", "api_key": "", "model": "",
                "codex": {"proxy": "http://127.0.0.1:1"},
            }, **{f"palette-{index}": {"engine":"codex", "codex":{"proxy":"http://127.0.0.1:1"}, "display_name":f"Profile {index + 1}", "api_key":""}
                  for index in range(1,7)}},
        }), encoding="utf-8")
        import desktop_app as app
        app.get_live_balance = lambda *args, **kwargs: None
        app.user_profile_memory = None
        project = Path(directory, "project")
        project.mkdir()
        (project / "example.py").write_text('print("local preview")\n', encoding="utf-8")
        app.CODE_WORKSPACES.grant("main", project)
        server = app.http.server.ThreadingHTTPServer(("127.0.0.1", 0), app.BridgeHandler)
        app.BRIDGE_PORT = server.server_port
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        environment = dict(os.environ, KID_BRIDGE_TOKEN=app.BRIDGE_TOKEN, KID_BRIDGE_PORT=str(server.server_port))
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            frontend_port = reservation.getsockname()[1]
        origin = f"http://127.0.0.1:{frontend_port}"
        with tempfile.TemporaryFile() as log:
            frontend = subprocess.Popen([
                "node", "node_modules/next/dist/bin/next", "start", "--hostname", "127.0.0.1", "--port", str(frontend_port),
            ], cwd=ROOT / "UI", env=environment, stdout=log, stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                for attempt in range(100):
                    if frontend.poll() is not None:
                        raise RuntimeError("Frontend exited before startup")
                    try:
                        urllib.request.urlopen(origin, timeout=1).close()
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError("Frontend did not start")
                # Exercise the production Next-to-Python route, not only the
                # browser fixture forwarding used below. All traffic is loopback.
                local=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                req=urllib.request.Request(origin+"/api/notes",headers={"Host":"127.0.0.1:3000"})
                with local.open(req,timeout=10) as response:
                    assert response.status==200 and "notes" in json.load(response)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(viewport={"width": 1440, "height": 1000})
                    api_requests = []
                    preview_codex_controls = [False]
                    preview_authenticated = [False]
                    preview_chat = [False]
                    pending_chat = [None]
                    activity_steps = ["Проверяю файлы проекта", "Команда: python -m unittest\nПроверки завершились успешно"]
                    quota_percent = [80]
                    def preview_limits():
                        return {"windows": [{"bucket":"codex", "slot":"primary", "duration_minutes":300,
                                              "remaining_percent":quota_percent[0], "resets_at":None}],
                                "updated_at":time.time(), "stale":False}
                    fail_notes_read = [False]
                    fail_delete = [False]
                    note_saves = []
                    def local_route(route):
                        if not route.request.url.startswith(origin + "/"):
                            route.abort()
                            return
                        url = urlsplit(route.request.url)
                        if not url.path.startswith("/api/"):
                            route.continue_()
                            return
                        api_requests.append(url.path)
                        if preview_chat[0] and url.path == "/api/chat":
                            pending_chat[0] = route
                            return
                        if preview_chat[0] and url.path == "/api/progress":
                            route.fulfill(status=200, content_type="application/json", body=json.dumps({"active":True,"steps":activity_steps,"reply":""}))
                            return
                        if fail_delete[0] and url.path == "/api/delete_persona":
                            route.fulfill(status=503, content_type="application/json", body='{"error":"test delete failure"}')
                            return
                        if preview_codex_controls[0] and url.path == "/api/codex/limits":
                            route.fulfill(status=200, content_type="application/json", body=json.dumps({"limits":preview_limits()}))
                            return
                        if url.path == "/api/notes" and route.request.method == "POST":
                            note_saves.append(True)
                        if fail_notes_read[0] and url.path == "/api/notes" and route.request.method == "GET":
                            route.fulfill(status=503, content_type="application/json", body='{"error":"test read failure"}')
                            return
                        if preview_codex_controls[0] and url.path == "/api/codex/status":
                            # UI-only fixture: no login/model request is sent.
                            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                                "available": True, "proxy_state": "verified", "auth_state": "authenticated" if preview_authenticated[0] else "login_pending", "error": "",
                                "reason": "Тестовое состояние интерфейса", "exit_ip": "192.0.2.1",
                                "login": None if preview_authenticated[0] else {"verification_url": "https://auth.openai.com/codex/device", "user_code": "TEST-CODE", "login_id": "ui-only"},
                                "capabilities": {"login": True, "chat": preview_authenticated[0]}, "limits": preview_limits(),
                                "models": [{"id": "ui-model", "model": "ui-model", "displayName": "UI test model", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}],
                            }))
                            return
                        headers = {"X-Kid-Token": app.BRIDGE_TOKEN}
                        if route.request.headers.get("content-type"):
                            headers["Content-Type"] = route.request.headers["content-type"]
                        # stdlib avoids Playwright embedding the private bridge
                        # header in exception call logs during browser shutdown.
                        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                        try:
                            connection.request(route.request.method, url.path + ("?" + url.query if url.query else ""),
                                               body=route.request.post_data_buffer, headers=headers)
                            response = connection.getresponse()
                            route.fulfill(status=response.status, content_type="application/json", body=response.read())
                        finally:
                            connection.close()
                    context.route("**/*", local_route)
                    page = context.new_page()
                    def capture(name):
                        if os.environ.get("KID_UI_SCREENSHOTS"):
                            destination = Path(os.environ["KID_UI_SCREENSHOTS"])
                            destination.mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=str(destination / (name + ".png")), animations="disabled")
                    errors = []
                    responses = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("response", lambda response: responses.append((response.status, response.url)))
                    page.on("requestfailed", lambda request: responses.append((request.failure, request.url)))
                    page.goto(origin)
                    try:
                        expect(page.get_by_text("Прокси: заблокировано", exact=False)).to_be_visible(timeout=15000)
                    except AssertionError:
                        print("Browser page errors:", errors)
                        print("Browser responses:", responses)
                        raise
                    draft = page.get_by_placeholder("Message your assistant...")
                    draft.fill("keep draft")
                    draft.press("Enter")
                    expect(page.get_by_text("Прокси недоступен. Запрос Codex заблокирован", exact=True)).to_be_visible()
                    expect(draft).to_have_value("keep draft")
                    assert "/api/chat" not in api_requests and "/api/upload" not in api_requests
                    assert api_requests.count("/api/init") == 1, "Startup must not load all chat history twice for notes"
                    assert "/api/notes" in api_requests
                    assert not note_saves, "Reading initial notes must not rewrite the server snapshot"
                    # A server-side write failure must remain visible, retain
                    # the local copy, and not poison subsequent saves.
                    page.get_by_role("button", name="Toggle notes", exact=True).click()
                    original_save_notes = app.save_notes
                    def fail_note_save(notes):
                        raise OSError("test disk failure")
                    app.save_notes = fail_note_save
                    try:
                        page.get_by_placeholder("Заголовок (необязательно)").fill("Retained note")
                        page.get_by_placeholder("Текст заметки...").fill("saved locally after server error")
                        page.get_by_role("button", name="Добавить", exact=True).click()
                        message = page.get_by_text("Не удалось сохранить заметки на сервере. Локальная копия сохранена.", exact=True)
                        expect(message).to_be_visible()
                        assert "Retained note" in page.evaluate("localStorage.getItem('rab_assistant_notes') || ''")
                        expect(message).to_be_hidden(timeout=10000)
                    finally:
                        app.save_notes = original_save_notes
                    page.get_by_placeholder("Заголовок (необязательно)").fill("Recovery note")
                    page.get_by_placeholder("Текст заметки...").fill("server recovered")
                    with page.expect_response(lambda response: "/api/notes" in response.url and response.request.method == "POST") as saved:
                        page.get_by_role("button", name="Добавить", exact=True).click()
                    assert saved.value.status == 200
                    assert len(app.load_notes()) == 2
                    page.get_by_role("button", name="Close notes", exact=True).click()
                    page.get_by_role("button", name="Файлы и изменения проекта").click()
                    page.get_by_role("button", name="example.py", exact=True).click()
                    expect(page.locator("pre").filter(has_text="local preview")).to_be_visible()
                    capture("codex-project-desktop")
                    page.set_viewport_size({"width": 390, "height": 844})
                    expect(page.get_by_role("button", name="Закрыть проект", exact=True)).to_be_visible()
                    assert page.locator('[data-slot="sheet-content"]').evaluate("e => e.scrollWidth <= e.clientWidth + 1"), "Project panel overflows on mobile"
                    capture("codex-project-mobile")
                    page.get_by_role("button", name="Закрыть проект", exact=True).click()
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    page.get_by_role("button", name="Open menu", exact=True).click()
                    active_profile = page.get_by_role("group", name="Профиль Codex test", exact=True)
                    expect(active_profile).to_have_attribute("data-active", "true")
                    assert active_profile.get_by_role("button").count() == 4
                    assert active_profile.evaluate("e => parseFloat(getComputedStyle(e).borderTopLeftRadius) >= e.clientHeight / 2")
                    colors = page.locator('[data-profile-id]').evaluate_all("rows => rows.map(row => getComputedStyle(row.querySelector('button')).backgroundColor)")
                    assert len(colors) == 7 and len(set(colors)) == 7, "Seven profiles must have distinct muted accents"
                    capture("profile-capsule-desktop")
                    page.set_viewport_size({"width":390,"height":844})
                    assert page.locator('[data-slot="sheet-content"]').evaluate("e => e.scrollWidth <= e.clientWidth + 1")
                    capture("profile-capsule-mobile")
                    page.set_viewport_size({"width":1440,"height":1000})
                    page.get_by_role("button", name="Настройки профиля Codex test").click()
                    expect(page.get_by_label("Движок", exact=True)).to_contain_text("Codex")
                    expect(page.get_by_label("Адрес прокси", exact=True)).to_have_value("127.0.0.1")
                    expect(page.get_by_label("Порт", exact=True)).to_have_value("1")
                    page.get_by_role("button", name="Лавандовый", exact=True).click()
                    page.get_by_role("switch", name="Автоматически разрешать команды", exact=True).check()
                    expect(page.get_by_role("button", name="Лавандовый", exact=True)).to_have_attribute("aria-pressed", "true")
                    page.get_by_label("Порт", exact=True).fill("1234")
                    page.get_by_label("Логин прокси", exact=True).fill("ui:user@example")
                    expect(page.get_by_role("button", name="Войти в ChatGPT")).to_be_disabled()
                    page.get_by_label("Поисковый движок", exact=True).click()
                    expect(page.get_by_role("option", name="Поиск kid", exact=True)).to_be_visible()
                    page.get_by_role("option", name="Поиск kid", exact=True).click()
                    expect(page.get_by_label("Поисковый движок", exact=True)).to_contain_text("Поиск kid")
                    capture("codex-settings-desktop")
                    page.set_viewport_size({"width": 390, "height": 844})
                    capture("codex-settings-mobile")
                    assert page.locator('[data-slot="dialog-content"]').evaluate("e => e.scrollWidth <= e.clientWidth + 1"), "Settings overflow on mobile"
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    page.get_by_label("Пароль прокси", exact=True).fill("ui-test-secret")
                    expect(page.get_by_label("Пароль прокси", exact=True)).to_have_attribute("type", "password")
                    page.get_by_role("button", name="Показать пароль прокси", exact=True).click()
                    expect(page.get_by_label("Пароль прокси", exact=True)).to_have_attribute("type", "text")
                    expect(page.get_by_label("Пароль прокси", exact=True)).to_have_value("ui-test-secret")
                    page.get_by_role("button", name="Скрыть пароль прокси", exact=True).click()
                    expect(page.get_by_label("Пароль прокси", exact=True)).to_have_attribute("type", "password")
                    page.get_by_role("button", name="Save changes", exact=True).click()
                    expect(page.get_by_text("Profile updated", exact=True)).to_be_visible()
                    assert app.load_config()["profiles"]["main"]["codex"]["proxy_password"] == "ui-test-secret"
                    assert app.load_config()["profiles"]["main"]["color"] == "lavender"
                    assert app.load_config()["profiles"]["main"]["codex"]["auto_approve_commands"] is True
                    assert app.load_config()["profiles"]["main"]["codex"]["proxy"] == "http://ui%3Auser%40example@127.0.0.1:1234"
                    before_reload_saves = len(note_saves)
                    fail_notes_read[0] = True
                    page.reload()
                    page.get_by_role("button", name="Open menu", exact=True).click()
                    page.get_by_role("button", name="Настройки профиля Codex test").click()
                    expect(page.get_by_label("Пароль прокси", exact=True)).to_have_value("")
                    expect(page.get_by_label("Пароль прокси", exact=True)).to_have_attribute("placeholder", "********")
                    expect(page.get_by_role("button", name="Лавандовый", exact=True)).to_have_attribute("aria-pressed", "true")
                    page.get_by_role("button", name="Показать пароль прокси", exact=True).click()
                    expect(page.get_by_label("Пароль прокси", exact=True)).to_have_value("ui-test-secret")
                    page.get_by_role("button", name="Скрыть пароль прокси", exact=True).click()
                    expect(page.get_by_label("Пароль прокси", exact=True)).to_have_value("")
                    page.get_by_label("Движок", exact=True).click()
                    page.get_by_role("option", name="API — OpenAI-совместимый", exact=True).click()
                    expect(page.get_by_label("Base URL", exact=True)).to_be_visible()
                    preview_codex_controls[0] = True
                    page.get_by_label("Движок", exact=True).click()
                    page.get_by_role("option", name="Codex — изолированный App Server", exact=True).click()
                    expect(page.get_by_label("Адрес прокси", exact=True)).to_have_value("127.0.0.1")
                    expect(page.get_by_label("Порт", exact=True)).to_have_value("1234")
                    expect(page.get_by_label("Логин прокси", exact=True)).to_have_value("ui:user@example")
                    expect(page.get_by_text("TEST-CODE", exact=True)).to_be_visible()
                    page.get_by_label("Модель Codex", exact=True).click()
                    page.get_by_role("option", name="UI test model", exact=True).click()
                    page.get_by_label("Уровень рассуждений", exact=True).click()
                    page.get_by_role("option", name="high", exact=True).click()
                    expect(page.get_by_label("Уровень рассуждений", exact=True)).to_contain_text("high")
                    capture("codex-controls-desktop")
                    page.set_viewport_size({"width": 390, "height": 844})
                    assert page.locator('[data-slot="dialog-content"]').evaluate("e => e.scrollWidth <= e.clientWidth + 1")
                    capture("codex-controls-mobile")
                    quota_percent[0] = 65
                    expect(page.get_by_text("codex: 65%", exact=False)).to_be_visible(timeout=12000)
                    assert "/api/codex/limits" in api_requests, "Quota must update automatically from the local cache"
                    assert len(note_saves) == before_reload_saves, "Cached notes after failed GET must not overwrite the server"
                    assert len(app.load_notes()) == 2
                    assert not errors, errors
                    preview_authenticated[0] = True
                    expect(page.get_by_text("Аккаунт сохранён. При следующем запуске kid подключится автоматически.", exact=True)).to_be_visible(timeout=10000)
                    expect(page.get_by_role("button", name="Войти в ChatGPT", exact=True)).to_have_count(0)
                    expect(page.get_by_role("button", name="Подключить через прокси", exact=True)).to_have_count(0)
                    expect(page.get_by_role("button", name="Выйти", exact=True)).to_be_visible()
                    preview_chat[0] = True
                    page.get_by_role("button", name="Cancel", exact=True).click()
                    page.set_viewport_size({"width":1440,"height":1000})
                    draft.fill("Тест интерфейса ленты действий")
                    draft.press("Enter")
                    expect(page.get_by_text("Выполняется работа", exact=False)).to_be_visible(timeout=10000)
                    expect(page.get_by_text("Проверяю файлы проекта", exact=True)).to_be_visible(timeout=10000)
                    capture("codex-activity-live")
                    assert pending_chat[0] is not None
                    pending_chat[0].fulfill(status=200, content_type="application/json", body=json.dumps({
                        "reply":"Проверка завершена. Все тесты прошли.","thinking":activity_steps,
                        "activity":{"duration_ms":97000,"status":"completed"}}))
                    expect(page.get_by_text("Проверка завершена. Все тесты прошли.", exact=True)).to_be_visible()
                    expect(page.get_by_text("Проверяю файлы проекта", exact=True)).not_to_be_visible()
                    capture("codex-activity-completed")
                    page.get_by_text("Работа завершена", exact=False).click()
                    expect(page.get_by_text("Проверяю файлы проекта", exact=True)).to_be_visible()
                    # Persist the fixture exactly as a real Codex response and
                    # reload through /api/init: presentation must survive restart.
                    chat_id=json.loads(pending_chat[0].request.post_data)["session_id"]
                    app.save_chat_history(chat_id,[{"role":"user","content":"Тест интерфейса ленты действий"},
                        {"role":"assistant","content":"Проверка завершена. Все тесты прошли.","codex_operation":"ui-fixture",
                         "thinking":activity_steps,"activity":{"duration_ms":97000,"status":"completed"}}])
                    preview_chat[0] = False
                    page.reload()
                    expect(page.get_by_text("Работа завершена",exact=False)).to_be_visible(timeout=10000)
                    expect(page.get_by_text("Проверка завершена. Все тесты прошли.",exact=True)).to_have_count(1)
                    page.get_by_text("Работа завершена",exact=False).click()
                    expect(page.get_by_text("Проверяю файлы проекта",exact=True)).to_be_visible()
                    page.get_by_role("button", name="Open menu", exact=True).click()
                    page.get_by_role("button", name="Настройки профиля Codex test").click()
                    page.get_by_role("button", name="Удалить профиль", exact=True).click()
                    confirm = page.get_by_role("alertdialog")
                    expect(confirm).to_be_visible()
                    confirm.get_by_role("button", name="Отмена", exact=True).click()
                    assert "/api/delete_persona" not in api_requests
                    page.get_by_role("button", name="Удалить профиль", exact=True).click()
                    fail_delete[0] = True
                    confirm.get_by_role("button", name="Удалить", exact=True).click()
                    expect(page.get_by_text("test delete failure", exact=True)).to_be_visible()
                    expect(confirm).to_be_visible()
                    assert "main" in app.load_config()["profiles"]
                    fail_delete[0] = False
                    confirm.get_by_role("button", name="Удалить", exact=True).click()
                    expect(page.get_by_role("dialog", name="Profile & system prompt manager")).not_to_be_visible()
                    assert "main" not in app.load_config()["profiles"]
                    page.goto("about:blank")
                    context.unroute_all(behavior="wait")
                    browser.close()
                    print("UI smoke passed: notes write failure/recovery, read-only startup/cache fallback, single init, blocked auth/send, preserved draft, quota, files, profiles, write-only password, engine switch")
            finally:
                frontend.terminate()
                try:
                    frontend.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    frontend.kill()
                    frontend.wait(timeout=5)
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)
                if sys.exc_info()[0] is not None:
                    log.seek(0)
                    print("Frontend log:", log.read(8000).decode("utf-8", errors="replace"))


if __name__ == "__main__":
    main()
