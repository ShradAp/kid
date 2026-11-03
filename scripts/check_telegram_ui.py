"""Offline browser smoke test: real settings UI + authenticated bridge, disposable data."""

import http.client
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
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    from playwright.sync_api import sync_playwright, expect

    with tempfile.TemporaryDirectory(prefix="kid-telegram-ui-") as directory:
        os.environ["KID_CONFIG_DIR"] = directory
        Path(directory, "config.json").write_text(json.dumps({"active_profile": "main", "profiles": {
            "main": {"engine": "codex", "display_name": "Codex fixture", "api_key": "", "model": ""}}}), encoding="utf-8")
        import desktop_app as app
        app.user_profile_memory = None
        server = app.http.server.ThreadingHTTPServer(("127.0.0.1", 0), app.BridgeHandler)
        app.BRIDGE_PORT = server.server_port
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        with tempfile.TemporaryFile() as log:
            frontend = subprocess.Popen(["node", "node_modules/next/dist/bin/next", "start", "--hostname", "127.0.0.1", "--port", str(port)],
                                        cwd=ROOT / "UI", stdout=log, stderr=log,
                                        env={**os.environ, "KID_BRIDGE_TOKEN": app.BRIDGE_TOKEN, "KID_BRIDGE_PORT": str(server.server_port)},
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    try:
                        opener.open(origin, timeout=1).close()
                        break
                    except OSError:
                        if frontend.poll() is not None:
                            raise RuntimeError("Frontend exited")
                        time.sleep(.1)
                else:
                    raise RuntimeError("Frontend startup timed out")
                # Verify production Next route, not only browser fixture forwarding.
                req = urllib.request.Request(origin + "/api/telegram/settings", headers={"Host": "127.0.0.1:3000"})
                with opener.open(req, timeout=5) as response:
                    assert not json.load(response)["enabled"]
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(viewport={"width": 1280, "height": 800})
                    errors = []
                    def route_local(route):
                        if not route.request.url.startswith(origin + "/"):
                            route.abort()
                            return
                        url = urlsplit(route.request.url)
                        if not url.path.startswith("/api/"):
                            route.continue_()
                            return
                        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=30)
                        try:
                            headers = {"X-Kid-Token": app.BRIDGE_TOKEN}
                            if route.request.headers.get("content-type"):
                                headers["Content-Type"] = route.request.headers["content-type"]
                            connection.request(route.request.method, url.path + ("?" + url.query if url.query else ""),
                                               body=route.request.post_data_buffer, headers=headers)
                            response = connection.getresponse()
                            route.fulfill(status=response.status, content_type=response.getheader("Content-Type", "application/json"), body=response.read())
                        finally:
                            connection.close()
                    context.route("**/*", route_local)
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(origin)
                    page.get_by_role("button", name="Open menu", exact=True).click()
                    page.get_by_role("button", name="Settings", exact=True).click()
                    page.get_by_role("button", name="Telegram", exact=True).click()
                    expect(page.get_by_label("Ваш числовой Telegram ID")).to_be_visible()
                    expect(page.get_by_role("switch", name="Живой статус запроса", exact=True)).to_be_checked()
                    expect(page.get_by_role("switch", name="Постепенное появление ответа", exact=True)).to_be_checked()
                    page.get_by_role("switch", name="Уведомления о запуске и восстановлении связи", exact=True).click()
                    expect(page.get_by_role("button", name="Открыть /panel в Telegram", exact=True)).to_be_disabled()
                    token = "123456:" + "offline_fixture_" * 3
                    page.get_by_label("Токен Telegram", exact=True).fill(token)
                    page.get_by_label("Ваш числовой Telegram ID").fill("123456789")
                    page.get_by_label("Профиль Codex", exact=True).select_option("main")
                    page.get_by_role("button", name="Сохранить настройки Telegram", exact=True).click()
                    expect(page.get_by_text("Настройки сохранены.", exact=True)).to_be_visible()
                    settings = json.loads(Path(directory, "telegram.json").read_text(encoding="utf-8"))
                    assert settings["token"] == token and settings["enabled"] is False
                    assert settings["notifications"] is False and settings["streaming"] is True and settings["live_status"] is True
                    assert not app.TELEGRAM.threads
                    # Reopen the section: persisted token must not be returned to the form.
                    page.get_by_role("button", name="Telegram", exact=True).click()
                    page.get_by_role("button", name="Telegram", exact=True).click()
                    expect(page.get_by_label("Токен Telegram", exact=True)).to_have_value("")
                    expect(page.get_by_role("switch", name="Уведомления о запуске и восстановлении связи", exact=True)).not_to_be_checked()
                    expect(page.get_by_text("Токен сохранён.", exact=False)).to_be_visible()
                    app.TELEGRAM.store.change(lambda state: state["history"].append({"role": "user", "content": "fixture"}))
                    page.get_by_role("button", name="Очистить переписку", exact=True).click()
                    page.get_by_role("button", name="Подтвердить очистку", exact=True).click()
                    expect(page.get_by_text("Переписка очищена, контекст сброшен.", exact=True)).to_be_visible()
                    assert app.TELEGRAM.store.state()["history"] == []
                    incoming = app.TELEGRAM.files.incoming
                    incoming.mkdir(parents=True, exist_ok=True)
                    archived_file = incoming / ("a" * 32 + "_fixture.txt")
                    archived_file.write_text("archive fixture", encoding="utf-8")
                    app.TELEGRAM.files.archive.register(app.TELEGRAM.store.settings(), archived_file, "fixture.txt")
                    page.get_by_role("button", name="Удалить архив входящих файлов", exact=True).click()
                    expect(page.get_by_text("Сохранённые входящие файлы этого бота и владельца", exact=False)).to_be_visible()
                    page.get_by_role("button", name="Подтвердить удаление архива", exact=True).click()
                    expect(page.get_by_text("Архив входящих файлов удалён, контекст сброшен.", exact=True)).to_be_visible()
                    assert not archived_file.exists()
                    assert app.TELEGRAM.public_settings()["archive_count"] == 0
                    assert not errors, errors
                    assert page.get_by_role("dialog").evaluate("element => element.scrollHeight >= element.clientHeight")
                    browser.close()
                print("PASS: global settings, local persistence, token masking, clear, Next bridge; no external requests")
            finally:
                frontend.terminate()
                frontend.wait(timeout=10)
                server.shutdown()
                server.server_close()
                app.TELEGRAM.close(timeout=1)


if __name__ == "__main__":
    main()
