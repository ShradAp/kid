"""Exercise the local library/profile UI against a temporary authenticated backend.

Run after UI production build. No real account, external network or Codex launch.
"""
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

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory(prefix="kid-extensions-ui-") as temporary:
        os.environ["KID_CONFIG_DIR"] = temporary
        root = Path(temporary)
        (root / "config.json").write_text(json.dumps({"active_profile": "a", "proxy_mode": "custom",
            "proxy_url": "http://127.0.0.1:1", "profiles": {
                "a": {"engine": "codex", "display_name": "Profile A"},
                "b": {"engine": "codex", "display_name": "Profile B"}}}), encoding="utf-8")
        skill = root / "pdf-skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: PDF helper\ndescription: Чтение и обработка PDF\n---\nRead instructions only when needed.\n", encoding="utf-8")
        fixture = root / "echo.py"
        fixture.write_text('''import json, os, sys
assert os.environ.get("API_KEY") == "UI_SECRET_VALUE"
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request: continue
    if request["method"] == "initialize":
        result = {"protocolVersion":"2025-06-18", "capabilities":{"tools":{}}, "serverInfo":{"name":"echo", "version":"1"}}
    else:
        result = {"tools":[{"name":"echo", "description":"Echo text", "inputSchema":{"type":"object", "properties":{}}}]}
    print(json.dumps({"jsonrpc":"2.0", "id":request["id"], "result":result}), flush=True)
''', encoding="utf-8")
        manifest = root / "mcp.json"
        manifest.write_text(json.dumps({"name": "Echo server", "description": "Проверка MCP", "mcp_servers": {
            "echo": {"command": sys.executable, "args": [str(fixture)], "env_from": {"API_KEY": "UI_MCP_KEY"}}}}), encoding="utf-8")
        import desktop_app as app
        app.get_live_balance = lambda *args, **kwargs: None
        app.user_profile_memory = None
        app.BridgeHandler.log_message = lambda *args: None
        # UI status fixture never starts a real protected runtime.
        app._connect_startup_codex = lambda: None
        app.CODEX_SERVICE.status = lambda *args: {"available": False, "auth_state": "not_authenticated", "models": [],
            "capabilities": {"login": False, "chat": False}, "reason": "Local UI test"}
        server = app.http.server.ThreadingHTTPServer(("127.0.0.1", 0), app.BridgeHandler)
        app.BRIDGE_PORT = server.server_port
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            frontend_port = reservation.getsockname()[1]
        origin = f"http://127.0.0.1:{frontend_port}"
        environment = dict(os.environ, KID_BRIDGE_TOKEN=app.BRIDGE_TOKEN, KID_BRIDGE_PORT=str(server.server_port))
        with tempfile.TemporaryFile() as log:
            frontend = subprocess.Popen(["node", "node_modules/next/dist/bin/next", "start", "--hostname", "127.0.0.1", "--port", str(frontend_port)],
                cwd=ROOT / "UI", env=environment, stdout=log, stderr=log,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    if frontend.poll() is not None:
                        raise RuntimeError("Frontend exited")
                    try:
                        local.open(origin, timeout=1).close()
                        break
                    except OSError:
                        time.sleep(.1)
                # Verify the real Next.js proxy accepts the new endpoint.
                req = urllib.request.Request(origin + "/api/extensions", headers={"Host": "127.0.0.1:3000"})
                with local.open(req, timeout=5) as response:
                    assert json.load(response)["extensions"] == []
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    def forward(route):
                        if not route.request.url.startswith(origin + "/"):
                            route.abort()
                            return
                        url = urlsplit(route.request.url)
                        if not url.path.startswith("/api/"):
                            route.continue_()
                            return
                        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                        try:
                            connection.request(route.request.method, url.path + ("?" + url.query if url.query else ""),
                                body=route.request.post_data_buffer, headers={"X-Kid-Token": app.BRIDGE_TOKEN, "Content-Type": "application/json"})
                            response = connection.getresponse()
                            route.fulfill(status=response.status, content_type="application/json", body=response.read())
                        finally:
                            connection.close()
                    page.route("**/*", forward)
                    page.goto(origin)
                    page.get_by_role("button", name="Open menu", exact=True).click()
                    page.get_by_role("button", name="Settings", exact=True).click()
                    page.get_by_role("button", name="Расширения", exact=True).click()
                    page.get_by_label("Источник", exact=True).fill(str(skill))
                    page.get_by_role("button", name="Проверить источник и состав", exact=True).click()
                    expect(page.get_by_role("button", name="Добавить в библиотеку", exact=True)).to_be_visible()
                    assert not app.load_config().get("extensions"), "Preview must not install"
                    page.get_by_role("button", name="Добавить в библиотеку", exact=True).click()
                    expect(page.get_by_text("PDF helper", exact=True)).to_be_visible()
                    entry = app.load_config()["extensions"][0]
                    assert all(not profile.get("extension_ids") for profile in app.load_config()["profiles"].values())
                    page.locator("select").select_option("mcp")
                    page.get_by_label("Источник", exact=True).fill(str(manifest))
                    page.get_by_role("button", name="Проверить источник и состав", exact=True).click()
                    page.get_by_role("button", name="Добавить в библиотеку", exact=True).click()
                    expect(page.get_by_text("Echo server", exact=True)).to_be_visible()
                    mcp_entry = app.load_config()["extensions"][-1]
                    row = page.locator('[data-extension-id="' + mcp_entry["id"] + '"]')
                    expect(row).to_contain_text("Требует настройки")
                    row.get_by_role("button", name="Настроить", exact=True).click()
                    page.get_by_label("UI_MCP_KEY", exact=True).fill("UI_SECRET_VALUE")
                    page.get_by_role("button", name="Сохранить настройку", exact=True).click()
                    row.get_by_role("button", name="Запустить и проверить MCP", exact=True).click()
                    expect(row).to_contain_text("Готово")
                    assert "UI_SECRET_VALUE" not in page.locator("body").inner_text()
                    page.keyboard.press("Escape")
                    page.get_by_role("button", name="Open menu", exact=True).click()
                    page.get_by_role("button", name="Настройки профиля Profile A", exact=True).click()
                    checkbox = page.get_by_role("checkbox", name="Включить PDF helper", exact=True)
                    expect(checkbox).not_to_be_checked()
                    checkbox.check()
                    expect(checkbox).to_be_enabled()
                    assert app.load_config()["profiles"]["a"]["extension_ids"] == [entry["id"]]
                    # Emulate a model install while the profile dialog remains open.
                    from services.skill_imports import install_skill
                    live_folder = root / "live-skill"
                    live_folder.mkdir()
                    (live_folder / "SKILL.md").write_text("---\nname: Live helper\ndescription: Live library update test\n---\nRead on demand.\n", encoding="utf-8")
                    inspected = install_skill(root, app.load_config, app.save_config, action="inspect", source=str(live_folder))
                    install_skill(root, app.load_config, app.save_config, action="install", source=str(live_folder), fingerprint=inspected["fingerprint"])
                    live_checkbox = page.get_by_role("checkbox", name="Включить Live helper", exact=True)
                    expect(live_checkbox).to_be_visible(timeout=6000)
                    expect(live_checkbox).not_to_be_checked()
                    assert app.load_config()["profiles"]["a"]["extension_ids"] == [entry["id"]]
                    page.keyboard.press("Escape")
                    page.get_by_role("button", name="Open menu", exact=True).click()
                    page.get_by_role("button", name="Настройки профиля Profile B", exact=True).click()
                    expect(page.get_by_role("checkbox", name="Включить PDF helper", exact=True)).not_to_be_checked()
                    mcp_checkbox = page.get_by_role("checkbox", name="Включить Echo server", exact=True)
                    mcp_checkbox.check()
                    expect(mcp_checkbox).to_be_enabled()
                    assert app.load_config()["profiles"]["b"]["extension_ids"] == [mcp_entry["id"]]
                    page.keyboard.press("Escape")
                    page.get_by_role("button", name="Open menu", exact=True).click()
                    page.get_by_role("button", name="Настройки профиля Profile B", exact=True).click()
                    expect(page.get_by_role("checkbox", name="Включить Echo server", exact=True)).to_be_checked()
                    page.set_viewport_size({"width": 390, "height": 844})
                    assert page.locator('[data-slot="dialog-content"]').evaluate("e => e.scrollWidth <= e.clientWidth + 1"), "Mobile overflow"
                    page.keyboard.press("Escape")
                    page.reload()
                    page.get_by_role("button", name="Open menu", exact=True).click()
                    page.get_by_role("button", name="Настройки профиля Profile A", exact=True).click()
                    expect(page.get_by_role("checkbox", name="Включить PDF helper", exact=True)).to_be_checked()
                    expect(page.get_by_role("checkbox", name="Включить Echo server", exact=True)).not_to_be_checked()
                    assert not errors, errors
                    browser.close()
                print("PASS: preview, skill + MCP install, secret configuration, MCP probe, live library refresh, immediate independent checkbox saves, persistence, mobile layout, real HTTP proxy")
            finally:
                frontend.terminate()
                frontend.wait(timeout=10)
                server.shutdown()
                server.server_close()
                app.CODEX_SERVICE.close()


if __name__ == "__main__":
    main()
